from __future__ import annotations

import hashlib
import json
import sys
from types import SimpleNamespace

import psutil
import pytest


def _other_score(_session, _np, _tokenizer, _request):
    """A module-level fake scorer so the second asset spec is process-picklable."""
    return 0.25, False, 2


def _other_spec(pins):
    from core.onnx_asset import OnnxAssetSpec

    return OnnxAssetSpec(
        asset_id="email-prompt-guard",
        input_policy_id="email-prompt-v1",
        required_heads=("injection",),
        min_self_test_vectors=1,
        pins=pins,
        display_name="email prompt guard",
        labels={"injection": ("safe", "injection")},
        rss_limit_bytes=1024,
        load_timeout_seconds=2.0,
        call_timeout_seconds=1.0,
        idle_seconds=5.0,
        score=_other_score,
    )


def _manifest(root, *, asset_id, policy, head):
    root.mkdir(parents=True, exist_ok=True)
    model = root / "model.onnx"
    tokenizer = root / "tokenizer.json"
    model.write_bytes(b"model")
    tokenizer.write_bytes(b"tokenizer")
    files = {
        path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in (model, tokenizer)
    }
    path = root / "manifest.json"
    path.write_text(json.dumps({
        "asset_id": asset_id,
        "version": "v1",
        "files": files,
        "heads": [{"name": head, "threshold": 0.5}],
        "input_policy_id": policy,
        "self_test": [{"owner": "synthetic", "expected_score": 0.25}],
    }), encoding="utf-8")
    return path


def _install_fake_runtime(monkeypatch):
    class Session:
        def run(self, *_args, **_kwargs):
            return [[[0.0, 0.0]]]

    onnx = SimpleNamespace(
        disable_telemetry_events=lambda: None,
        SessionOptions=lambda: SimpleNamespace(),
        ExecutionMode=SimpleNamespace(ORT_SEQUENTIAL="sequential"),
        InferenceSession=lambda *_args, **_kwargs: Session(),
    )
    monkeypatch.setitem(sys.modules, "onnxruntime", onnx)
    monkeypatch.setitem(
        sys.modules,
        "tokenizers",
        SimpleNamespace(Tokenizer=SimpleNamespace(from_file=lambda _path: object())),
    )


def test_second_asset_spec_verifies_self_tests_and_scores_without_corrections_code(monkeypatch, tmp_path):
    from core.onnx_asset import load_pinned_manifest, run_self_test
    from core import onnx_child

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    pins = {"v1": hashlib.sha256(path.read_bytes()).hexdigest()}
    spec = _other_spec(pins)
    _install_fake_runtime(monkeypatch)

    manifest = load_pinned_manifest(tmp_path, spec)
    run_self_test(tmp_path, spec)
    child, connection = _child_with(monkeypatch, manifest, spec, [
        {"ready": True},
        {"score": 0.25, "version": {"head": "injection"}, "truncated": False, "original_tokens": 2},
    ])

    child.ensure_loaded()
    reply = child.score({"owner": "synthetic", "head": "injection"})

    assert (manifest.asset_id, reply["score"], reply["version"]["head"]) == (
        "email-prompt-guard", 0.25, "injection",
    )
    assert connection.sent == [{"owner": "synthetic", "head": "injection"}]
    assert child.running
    assert onnx_child.RSS_POLL_SECONDS <= 0.05


def test_manifest_is_refused_by_the_wrong_asset_spec(tmp_path):
    from core.corrections_classifier import CORRECTIONS_SPEC
    from core.onnx_asset import AssetError, load_manifest

    other_path = _manifest(tmp_path / "other", asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    corrections_path = _manifest(
        tmp_path / "corrections",
        asset_id="corrections-classifier",
        policy="modernbert-owner-context-v2-2048",
        head="correction",
    )
    spec = _other_spec({"v1": hashlib.sha256(other_path.read_bytes()).hexdigest()})

    with pytest.raises(AssetError):
        load_manifest(other_path, CORRECTIONS_SPEC)
    with pytest.raises(AssetError):
        load_manifest(corrections_path, spec)


class _FakeConnection:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def poll(self, _timeout):
        return bool(self.replies)

    def recv(self):
        return self.replies.pop(0)

    def send(self, value):
        self.sent.append(value)

    def close(self):
        pass


class _FakeProcess:
    pid = 123

    def __init__(self, **_kwargs):
        self.killed = False

    def start(self):
        pass

    def is_alive(self):
        return not self.killed

    def terminate(self):
        self.killed = True

    def kill(self):
        self.killed = True

    def join(self, _timeout=None):
        pass


def _child_with(monkeypatch, manifest, spec, replies):
    from core import onnx_child

    connection = _FakeConnection(replies)
    monkeypatch.setattr(onnx_child, "Process", _FakeProcess)
    monkeypatch.setattr(onnx_child, "Pipe", lambda: (connection, _FakeConnection([])))
    monkeypatch.setattr(onnx_child, "_child_rss_bytes", lambda _process: 0)
    return onnx_child.OnnxChild(manifest, spec), connection


def test_parent_kills_a_child_that_exceeds_rss_before_a_call_reply(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}, {"score": 0.25}])
    readings = iter([0, 0, spec.rss_limit_bytes + 1])
    monkeypatch.setattr(onnx_child, "_child_rss_bytes", lambda _process: next(readings))

    child.ensure_loaded()
    with pytest.raises(onnx_child.OnnxMessageFailed, match="rss"):
        child.score({"owner": "synthetic", "head": "injection"})

    assert connection.sent == [{"owner": "synthetic", "head": "injection"}]
    assert connection.replies == [{"score": 0.25}]
    assert not child.running


def test_unreadable_memory_never_sends_an_uncapped_call(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}])
    readings = iter([0, psutil.AccessDenied(pid=123)])

    def unreadable(_process):
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(onnx_child, "_child_rss_bytes", unreadable)
    child.ensure_loaded()
    with pytest.raises(onnx_child.OnnxUnavailable, match="rss_unavailable") as raised:
        child.score({"owner": "synthetic", "head": "injection"})

    assert raised.value.kind == "transient"  # an outage, never charged to this input
    assert connection.sent == []
    assert not child.running


def test_unreadable_memory_during_load_is_an_outage_not_a_bad_version(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}])

    def missing(_process):
        raise psutil.AccessDenied(pid=123)

    monkeypatch.setattr(onnx_child, "_child_rss_bytes", missing)

    with pytest.raises(onnx_child.OnnxUnavailable, match="rss_unavailable") as raised:
        child.ensure_loaded()

    assert raised.value.kind == "transient"
    assert not child.running


def test_a_load_rss_breach_seen_by_the_parent_excludes_the_version(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}])
    monkeypatch.setattr(onnx_child, "_child_rss_bytes", lambda _process: spec.rss_limit_bytes + 1)

    with pytest.raises(onnx_child.OnnxUnavailable, match="^load rss$") as raised:
        child.ensure_loaded()

    assert raised.value.kind == "deterministic"  # same reason the child-side check reports
    assert not child.running


def test_a_child_that_exits_during_load_stays_a_transient_crash(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    class _ExitedConnection(_FakeConnection):
        def poll(self, _timeout):
            return True

        def recv(self):
            raise EOFError

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [])
    monkeypatch.setattr(onnx_child, "Pipe", lambda: (_ExitedConnection([]), _FakeConnection([])))

    def exited(_process):
        raise psutil.NoSuchProcess(pid=123)

    monkeypatch.setattr(onnx_child, "_child_rss_bytes", exited)

    with pytest.raises(onnx_child.OnnxUnavailable) as raised:
        child.ensure_loaded()

    assert (raised.value.kind, raised.value.cause) == ("transient", "child_exited")
    assert not child.running


def test_load_deadline_stays_a_transient_unavailable_error(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [])
    ticks = iter((0.0, spec.load_timeout_seconds + 0.1))
    monkeypatch.setattr(onnx_child.time, "monotonic", lambda: next(ticks))

    with pytest.raises(onnx_child.OnnxUnavailable, match="load timeout") as raised:
        child.ensure_loaded()

    assert raised.value.kind == "transient"
    assert not child.running


def test_load_waits_past_the_call_deadline_until_its_own(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    assert spec.call_timeout_seconds < spec.load_timeout_seconds - 0.01
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}])
    ticks = iter((0.0, spec.load_timeout_seconds - 0.01))
    monkeypatch.setattr(onnx_child.time, "monotonic", lambda: next(ticks))

    child.ensure_loaded()

    assert child.loaded


def test_call_deadline_stays_a_per_message_error(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(monkeypatch, load_manifest(path, spec), spec, [{"ready": True}])
    child.ensure_loaded()
    ticks = iter((0.0, spec.call_timeout_seconds + 0.1))
    monkeypatch.setattr(onnx_child.time, "monotonic", lambda: next(ticks))

    with pytest.raises(onnx_child.OnnxMessageFailed, match="timeout"):
        child.score({"owner": "synthetic", "head": "injection"})

    assert not child.running


def test_child_that_does_not_exit_stays_an_unavailable_error(monkeypatch, tmp_path):
    from core import onnx_child
    from core.onnx_asset import load_manifest

    class StubbornProcess(_FakeProcess):
        def is_alive(self):
            return True

        def terminate(self):
            pass

        def kill(self):
            pass

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()})
    child, _connection = _child_with(
        monkeypatch, load_manifest(path, spec), spec, [{"error": "timeout", "kind": "transient"}],
    )
    monkeypatch.setattr(onnx_child, "Process", StubbornProcess)
    monkeypatch.setattr(onnx_child, "CLOSE_JOIN_SECONDS", 0.0)

    with pytest.raises(onnx_child.OnnxUnavailable, match="child_did_not_exit") as raised:
        child.ensure_loaded()

    assert raised.value.kind == "transient"


def _hold_open_after_ready(connection, _manifest_path, _spec):
    """A module-level child target (picklable under spawn) that stays alive and silent."""
    connection.send({"ready": True})
    connection.poll(30)


def _loaded_real_child(tmp_path):
    import dataclasses

    from core.onnx_asset import load_manifest
    from core.onnx_child import OnnxChild

    path = _manifest(tmp_path, asset_id="email-prompt-guard", policy="email-prompt-v1", head="injection")
    spec = dataclasses.replace(
        _other_spec({"v1": hashlib.sha256(path.read_bytes()).hexdigest()}), rss_limit_bytes=1 << 40,
    )
    child = OnnxChild(load_manifest(path, spec), spec, target=_hold_open_after_ready)
    child.ensure_loaded()
    return child


def _peer_sees_eof_after_parent_closes(child_start_context, monkeypatch, tmp_path) -> bool:
    import socket

    from core import onnx_child

    monkeypatch.setattr(onnx_child, "Process", child_start_context.Process)
    monkeypatch.setattr(onnx_child, "Pipe", child_start_context.Pipe)
    parent_end, peer_end = socket.socketpair()
    peer_end.settimeout(2.0)
    child = None
    try:
        # The child starts while the parent holds a live socket, as the backend does
        # with its listener and every open HTTP or websocket connection.
        child = _loaded_real_child(tmp_path)
        parent_end.close()
        try:
            return peer_end.recv(1) == b""
        except socket.timeout:
            return False
    finally:
        peer_end.close()
        if child is not None:
            child.close()


def test_onnx_child_does_not_inherit_parent_sockets(monkeypatch, tmp_path):
    """A parent socket closed while the child lives must still reach its peer as EOF."""
    from core import onnx_child

    assert _peer_sees_eof_after_parent_closes(
        onnx_child._SPAWN, monkeypatch, tmp_path,
    ), "the child kept a copy of the parent's socket open"


def test_fork_start_method_is_what_leaks_the_socket(monkeypatch, tmp_path):
    """Control: under fork the same child holds the socket open and the peer never sees EOF."""
    import multiprocessing

    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("this platform has no fork start method")

    assert not _peer_sees_eof_after_parent_closes(multiprocessing.get_context("fork"), monkeypatch, tmp_path)
