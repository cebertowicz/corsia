"""The raw-frame recorder must be off by default and never wildcard.

These fixtures are destined for a public repository, so the interesting cases
here are the refusals: an unset allowlist, a wildcard, and a thread nobody named
must all record nothing. A recorder that captures one thread more than it was
told to publishes the user's private working data.
"""

from __future__ import annotations

import json

from subagents.stream_capture import capture_dir_for, record_frame

_DIR_VAR = "CORSIA_PROVIDER_STREAM_CAPTURE_DIR"
_THREADS_VAR = "CORSIA_PROVIDER_STREAM_CAPTURE_THREADS"


def _configure(monkeypatch, tmp_path, sessions: str | None):
    monkeypatch.setenv(_DIR_VAR, str(tmp_path / "capture"))
    if sessions is None:
        monkeypatch.delenv(_THREADS_VAR, raising=False)
    else:
        monkeypatch.setenv(_THREADS_VAR, sessions)


def test_records_only_the_allowlisted_thread(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "scratch-thread-1")

    record_frame("scratch-thread-1", "cc", {"type": "assistant", "n": 1})
    record_frame("steves-real-thread", "cc", {"type": "assistant", "n": 2})

    captured = sorted(p.name for p in (tmp_path / "capture").iterdir())
    assert captured == ["cc-scratch-thread-1.jsonl"]

    lines = (tmp_path / "capture" / "cc-scratch-thread-1.jsonl").read_text()
    assert [json.loads(line) for line in lines.splitlines()] == [
        {"type": "assistant", "n": 1}
    ]


def test_disabled_when_no_directory_is_configured(monkeypatch, tmp_path):
    monkeypatch.delenv(_DIR_VAR, raising=False)
    monkeypatch.setenv(_THREADS_VAR, "scratch-thread-1")

    assert capture_dir_for("scratch-thread-1") is None
    record_frame("scratch-thread-1", "cc", {"type": "assistant"})
    assert not (tmp_path / "capture").exists()


def test_disabled_when_allowlist_is_absent_or_empty(monkeypatch, tmp_path):
    for sessions in (None, "", "   ", ","):
        _configure(monkeypatch, tmp_path, sessions)
        assert capture_dir_for("scratch-thread-1") is None


def test_allowlist_is_never_a_wildcard(monkeypatch, tmp_path):
    """A pattern must not widen the allowlist — it matches nothing at all."""
    for wildcard in ("*", "%", ".*", "scratch-*", "scratch-thread-?", "[a-z]+"):
        _configure(monkeypatch, tmp_path, wildcard)
        assert capture_dir_for("scratch-thread-1") is None
        assert capture_dir_for("steves-real-thread") is None


def test_a_wildcard_like_word_is_only_ever_a_literal_id(monkeypatch, tmp_path):
    """``all`` is a legal thread id, so it must match that thread and no other.

    Entries are compared by equality, never by match, so a word that reads like
    a wildcard cannot behave as one.
    """
    _configure(monkeypatch, tmp_path, "all")
    assert capture_dir_for("all") is not None
    assert capture_dir_for("scratch-thread-1") is None
    assert capture_dir_for("steves-real-thread") is None


def test_unknown_thread_id_is_never_recorded(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "scratch-thread-1")
    assert capture_dir_for(None) is None
    assert capture_dir_for("") is None
    assert capture_dir_for("scratch-thread-10") is None


def test_separates_harnesses_and_appends_in_order(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, "t-1,t-2")

    for index in range(3):
        record_frame("t-1", "cc", {"i": index})
    record_frame("t-2", "codex", {"method": "turn/started"})

    cc_lines = (tmp_path / "capture" / "cc-t-1.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in cc_lines] == [0, 1, 2]
    codex = (tmp_path / "capture" / "codex-t-2.jsonl").read_text()
    assert json.loads(codex) == {"method": "turn/started"}


class _FakeStdout:
    """Hands the read loop one line at a time, then EOF."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    async def readline(self) -> bytes:
        return self._lines.pop(0) if self._lines else b""


class _FakeProc:
    def __init__(self, lines: list[bytes]):
        self.stdout = _FakeStdout(lines)
        self.returncode = 0


async def test_cc_read_loop_records_every_raw_line(monkeypatch, tmp_path):
    """The tap must sit in the real read loop, not merely be importable.

    Drives ``CCStreamSession._read_loop`` over a fake stdout so the assertion
    covers the exact seam a fixture is recorded from. A helper-only test would
    still pass if the call site were never wired.
    """
    from subagents.cc_stream import CCStreamSession

    _configure(monkeypatch, tmp_path, "scratch-1")
    raw_lines = [
        b'{"type":"system","subtype":"init","session_id":"fake"}\n',
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}\n',
        b'not json at all\n',
        b'{"type":"result","subtype":"success","is_error":false}\n',
    ]
    session = CCStreamSession(
        binary="claude", session_id="cc-1", corsia_thread_id="scratch-1",
    )
    session._proc = _FakeProc(raw_lines)
    session._closing = True  # skip the exit handler; we only want the loop

    await session._read_loop()

    recorded = [
        json.loads(line)
        for line in (tmp_path / "capture" / "cc-scratch-1.jsonl")
        .read_text().splitlines()
    ]
    # Every parseable frame, verbatim and in order; the non-JSON line is
    # skipped by the loop before the tap, exactly as it is in production.
    assert [frame.get("type") for frame in recorded] == [
        "system", "assistant", "result",
    ]
    assert recorded[1]["message"]["content"][0]["text"] == "hi"


async def test_cc_read_loop_records_nothing_for_an_unlisted_thread(
    monkeypatch, tmp_path,
):
    from subagents.cc_stream import CCStreamSession

    _configure(monkeypatch, tmp_path, "scratch-1")
    session = CCStreamSession(
        binary="claude", session_id="cc-1",
        corsia_thread_id="steves-real-thread",
    )
    session._proc = _FakeProc([b'{"type":"assistant"}\n'])
    session._closing = True

    await session._read_loop()

    assert not (tmp_path / "capture").exists()


async def test_the_tap_cannot_kill_the_reader(monkeypatch, tmp_path):
    """A recorder must be incapable of tearing down the read loop.

    Regression: the first version read ``self.corsia_session_id`` directly at
    the call site. Sessions built with ``__new__`` — which the reader's own
    safety tests do — have no such attribute, so the access raised
    ``AttributeError``, the loop's outer handler treated it as a reader crash,
    and it killed Claude. A diagnostic tap that can end a turn is worse than no
    tap, so the call site must survive a session missing the attribute
    entirely.
    """
    from subagents.cc_stream import CCStreamSession

    _configure(monkeypatch, tmp_path, "scratch-1")
    killed = {"proc": False}
    seen: list[dict] = []

    class _Proc:
        returncode = None
        stderr = None

        def __init__(self):
            self.stdout = _FakeStdout([
                b'{"type":"assistant"}\n', b'{"type":"after"}\n',
            ])

        def kill(self):
            killed["proc"] = True

    async def _on_event(ev):
        seen.append(ev)

    monkeypatch.setattr("subagents.cc_stream.normalize_event", lambda raw: [raw])

    stream = CCStreamSession(binary="unused", session_id="capture-test")
    del stream.corsia_thread_id  # Keep the missing-attribution regression genuine.
    stream.session_id = "thread-alive"
    stream._proc = _Proc()
    stream._on_event = _on_event
    stream._closing = True
    stream._terminal_event = None
    stream._control_responses = {}

    await stream._read_loop()

    assert killed["proc"] is False, "the capture tap terminated Claude Code"
    assert {ev.get("type") for ev in seen} == {"assistant", "after"}
    # No thread id to attribute the frames to, so nothing may be recorded.
    assert not (tmp_path / "capture").exists()


async def test_the_codex_tap_cannot_kill_the_reader(monkeypatch, tmp_path):
    """Same invariant on the codex transport, which kills on any exception too.

    ``codex_app_server._read_loop`` wraps its dispatch in ``except Exception:
    … self._proc.kill()``, exactly like the CC reader. The CC regression above
    would not have caught a codex-side attribute error, and fixing both call
    sites while testing one is how half a fix looks like a whole one.
    """
    from subagents.codex_app_server import CodexAppServerSession

    _configure(monkeypatch, tmp_path, "scratch-1")
    killed = {"proc": False}

    class _Proc:
        returncode = None
        stderr = None

        def __init__(self):
            self.stdout = _FakeStdout([
                b'{"method":"turn/started","params":{}}\n',
                b'{"method":"turn/completed","params":{}}\n',
            ])

        def kill(self):
            killed["proc"] = True

    seen: list[str] = []

    async def _handle_notification(method, _params):
        seen.append(method)

    session = CodexAppServerSession(binary="unused", task=None)
    del session.corsia_thread_id  # Keep the missing-attribution regression genuine.
    session._proc = _Proc()
    session._on_raw_message = None
    session._pending = {}
    session._pending_methods = {}
    session._closing = True
    session._on_exit = None
    session._stderr = bytearray()
    session._handle_notification = _handle_notification

    await session._read_loop()

    assert killed["proc"] is False, "the capture tap terminated the codex reader"
    assert seen == ["turn/started", "turn/completed"]
    assert not (tmp_path / "capture").exists()


def test_frames_are_durable_before_the_process_dies(monkeypatch, tmp_path):
    """A capture must survive the process it recorded being killed.

    The handle is held open across frames, so this pins the flush: without it
    a crash mid-capture would lose the buffered tail, which is exactly the
    interrupted-turn shape these fixtures exist to reproduce.
    """
    _configure(monkeypatch, tmp_path, "t-1")
    for index in range(5):
        record_frame("t-1", "cc", {"i": index})

    # Read from a separate handle, without closing the writer.
    lines = (tmp_path / "capture" / "cc-t-1.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in lines] == [0, 1, 2, 3, 4]


def test_frame_order_is_preserved_exactly(monkeypatch, tmp_path):
    """Ordering is the fixture's integrity, so it is asserted, not assumed.

    A reordered capture would certify a pump against a sequence the provider
    never sent. This is why the write stays synchronous on the event-loop
    thread rather than being offloaded.
    """
    _configure(monkeypatch, tmp_path, "t-1")
    for index in range(200):
        record_frame("t-1", "cc", {"i": index})

    lines = (tmp_path / "capture" / "cc-t-1.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in lines] == list(range(200))


def test_a_reopened_capture_keeps_appending(monkeypatch, tmp_path):
    """Closing the handles must not truncate or lose an existing capture."""
    from subagents.stream_capture import close_captures

    _configure(monkeypatch, tmp_path, "t-1")
    record_frame("t-1", "cc", {"i": 0})
    close_captures()
    record_frame("t-1", "cc", {"i": 1})
    close_captures()

    lines = (tmp_path / "capture" / "cc-t-1.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in lines] == [0, 1]


def test_retained_descriptors_are_bounded(monkeypatch, tmp_path):
    """The handle cache must not grow one descriptor per recorded thread.

    A recorder that can exhaust the process's file descriptors degrades the
    thing it exists to observe.
    """
    from subagents import stream_capture

    stream_capture.close_captures()
    ids = [f"t-{index}" for index in range(stream_capture._MAX_HANDLES + 6)]
    _configure(monkeypatch, tmp_path, ",".join(ids))
    for thread_id in ids:
        record_frame(thread_id, "cc", {"id": thread_id})

    # Asserted against the number of threads recorded, NOT against
    # _MAX_HANDLES. Comparing the cache to the very constant that sizes it is
    # vacuous: raising the constant would satisfy it without evicting anything,
    # so the test could never fail for the reason it exists. What must hold is
    # that recording N threads does not retain N descriptors.
    assert len(stream_capture._HANDLES) < len(ids)
    assert len(stream_capture._HANDLES) <= stream_capture._MAX_HANDLES

    # Eviction closes a file; it must never lose or truncate a capture.
    stream_capture.close_captures()
    for thread_id in ids:
        written = (tmp_path / "capture" / f"cc-{thread_id}.jsonl").read_text()
        assert json.loads(written)["id"] == thread_id


def test_an_evicted_capture_reopens_and_appends(monkeypatch, tmp_path):
    """Writing again after eviction appends rather than starting over."""
    from subagents import stream_capture

    stream_capture.close_captures()
    ids = [f"t-{index}" for index in range(stream_capture._MAX_HANDLES + 3)]
    _configure(monkeypatch, tmp_path, ",".join(ids))

    record_frame("t-0", "cc", {"i": 0})          # opened first, evicted first
    for thread_id in ids[1:]:
        record_frame(thread_id, "cc", {"i": 0})
    record_frame("t-0", "cc", {"i": 1})          # forces a reopen

    stream_capture.close_captures()
    lines = (tmp_path / "capture" / "cc-t-0.jsonl").read_text().splitlines()
    assert [json.loads(line)["i"] for line in lines] == [0, 1]


def test_io_failure_never_raises_into_the_read_loop(monkeypatch, tmp_path):
    """A diagnostic recorder must not be able to end a turn."""
    _configure(monkeypatch, tmp_path, "t-1")
    # A frame carrying a non-serializable value is the realistic failure.
    record_frame("t-1", "cc", {"handle": object()})
    # And an unwritable destination.
    monkeypatch.setenv(_DIR_VAR, "/proc/nonexistent/capture")
    record_frame("t-1", "cc", {"ok": True})
