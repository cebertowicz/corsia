
from __future__ import annotations

import asyncio
import base64
import os
import json
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from core.media_projection import ValidatedImage, ValidatedMediaProjection

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _flag(cmd: list[str], name: str):
    if name in cmd:
        i = cmd.index(name)
        nxt = cmd[i + 1] if i + 1 < len(cmd) else None
        return nxt if (nxt is not None and not nxt.startswith("--")) else True
    return None


def _media_projection(*items: tuple[str, str, bytes]) -> ValidatedMediaProjection:
    images = tuple(
        ValidatedImage(
            attachment_id=attachment_id,
            mime_type=mime_type,
            data=data,
            sha256=str(index) * 64,
            byte_size=len(data),
        )
        for index, (attachment_id, mime_type, data) in enumerate(items, start=1)
    )
    return ValidatedMediaProjection(images, tuple(image.sha256 for image in images))


class TestBuildCmd:
    def _session(self, **kw):
        from subagents.cc_stream import CCStreamSession
        kw.setdefault("binary", "claude")
        kw.setdefault("session_id", "sess-1")
        return CCStreamSession(**kw)

    def test_core_flags(self):
        cmd = self._session().build_cmd()
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert _flag(cmd, "--input-format") == "stream-json"
        assert _flag(cmd, "--output-format") == "stream-json"
        assert "--verbose" in cmd
        assert "--include-partial-messages" in cmd
        assert "--include-hook-events" in cmd
        assert _flag(cmd, "--permission-mode") == "bypassPermissions"
        assert _flag(cmd, "--session-id") == "sess-1"

    def test_each_transport_gets_one_opaque_generation_uuid(self):
        from uuid import UUID

        first = self._session()
        second = self._session()

        assert str(UUID(first.transport_generation_id)) == (
            first.transport_generation_id
        )
        assert str(UUID(second.transport_generation_id)) == (
            second.transport_generation_id
        )
        assert first.transport_generation_id != second.transport_generation_id

    def test_corsia_thread_id_injected_into_env(self, monkeypatch):
        monkeypatch.delenv("CORSIA_THREAD_ID", raising=False)
        env = self._session(corsia_thread_id="corsia-abc")._build_env()
        assert env["CORSIA_THREAD_ID"] == "corsia-abc"

    def test_no_corsia_thread_id_means_no_env_var(self, monkeypatch):
        monkeypatch.delenv("CORSIA_THREAD_ID", raising=False)
        env = self._session()._build_env()
        assert "CORSIA_THREAD_ID" not in env

    def test_inherited_thread_id_stripped_when_unset(self, monkeypatch):
        monkeypatch.setenv("CORSIA_THREAD_ID", "parent-leak")
        env = self._session()._build_env()  # no corsia_thread_id
        assert "CORSIA_THREAD_ID" not in env

    def test_explicit_id_overrides_inherited(self, monkeypatch):
        monkeypatch.setenv("CORSIA_THREAD_ID", "parent-leak")
        env = self._session(corsia_thread_id="child-x")._build_env()
        assert env["CORSIA_THREAD_ID"] == "child-x"

    def test_spawn_env_disables_provider_auto_compaction_and_memory(self, monkeypatch):
        monkeypatch.delenv("DISABLE_AUTO_COMPACT", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", raising=False)
        env = self._session()._build_env()
        assert env["DISABLE_AUTO_COMPACT"] == "1"
        assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"

    def test_spawn_env_disables_silent_turn_reminder(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_SILENT_TURN_REMINDER", "1")
        env = self._session()._build_env()
        assert env["CLAUDE_CODE_SILENT_TURN_REMINDER"] == "0"

    def test_spawn_env_binds_explicit_transcript_root(self, monkeypatch, tmp_path):
        inherited = tmp_path / "inherited"
        configured = tmp_path / "configured"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(inherited))

        env = self._session(claude_dir=str(configured))._build_env()

        assert env["CLAUDE_CONFIG_DIR"] == str(configured)

    def test_spawn_env_removes_default_config_override(
        self,
        monkeypatch,
        tmp_path,
    ):
        default = str((Path.home() / ".claude").resolve())
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "inherited"))

        env = self._session(claude_dir=default)._build_env()

        assert "CLAUDE_CONFIG_DIR" not in env

    def test_seed_file_optional(self):
        with_seed = self._session(seed_file="/tmp/seed.txt").build_cmd()
        assert _flag(with_seed, "--append-system-prompt-file") == "/tmp/seed.txt"
        without = self._session().build_cmd()
        assert "--append-system-prompt-file" not in without

    def test_mcp_config_or_empty_config_adds_strict(self):
        cmd = self._session(mcp_config="/tmp/mcp.json").build_cmd()
        assert _flag(cmd, "--mcp-config") == "/tmp/mcp.json"
        assert _flag(cmd, "--tools") == "mcp__corsia__*"
        assert "--strict-mcp-config" in cmd
        without = self._session().build_cmd()
        assert _flag(without, "--mcp-config") == '{"mcpServers":{}}'
        assert _flag(without, "--tools") == ""
        assert "--disable-slash-commands" in without
        assert "--strict-mcp-config" in without

    def test_default_launch_disables_provider_native_tools_not_user_settings(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_DISABLE_BUNDLED_SKILLS", raising=False)
        cmd = self._session(mcp_config="/tmp/corsia-mcp.json").build_cmd()
        assert _flag(cmd, "--setting-sources") == "user,local"
        settings = json.loads(_flag(cmd, "--settings"))
        assert settings["disableAllHooks"] is True
        assert settings["disableBundledSkills"] is True
        assert settings["disableSkillShellExecution"] is True
        assert settings["claudeMdExcludes"] == [
            "**/CLAUDE.md",
            "**/CLAUDE.local.md",
            "**/rules/**",
        ]
        assert settings["autoMemoryEnabled"] is False
        assert _flag(cmd, "--tools") == "mcp__corsia__*"
        assert "--disable-slash-commands" in cmd
        env = self._session(mcp_config="/tmp/corsia-mcp.json")._build_env()
        assert env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] == "1"

    def test_corsia_mcp_config_is_private_loopback_client(
        self, monkeypatch,
    ):
        from subagents.harness_tool_bridge import write_cc_mcp_config

        monkeypatch.setenv("BACKEND_PORT", "3001")
        path = write_cc_mcp_config(
            thread_id="corsia-thread-1",
            tool_token="ephemeral-secret",
        )
        cfg = json.loads(open(path, encoding="utf-8").read())

        server = cfg["mcpServers"]["corsia"]
        assert server["command"] == sys.executable
        args = server["args"]
        assert args[0].endswith("corsia_mcp_server.py")
        assert args == [args[0]]
        assert server["env"]["CORSIA_CC_TOOL_URL"] == (
            "http://127.0.0.1:3001/api/cc/_tool-bridge/corsia-thread-1"
        )
        assert server["env"]["CORSIA_CC_TOOL_TOKEN"] == "ephemeral-secret"
        assert "ephemeral-secret" not in " ".join(args)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert server["alwaysLoad"] is True
        assert list(cfg["mcpServers"]) == ["corsia"]

    def test_model_optional(self):
        cmd = self._session(model="opus").build_cmd()
        assert _flag(cmd, "--model") == "opus"
        assert "--model" not in self._session().build_cmd()

    def test_permission_mode_override(self):
        cmd = self._session(permission_mode="acceptEdits").build_cmd()
        assert _flag(cmd, "--permission-mode") == "acceptEdits"

    def test_resume_replaces_session_id(self):
        cmd = self._session(resume_session_id="old-cc-id").build_cmd()
        assert _flag(cmd, "--resume") == "old-cc-id"
        assert "--session-id" not in cmd
        fresh = self._session().build_cmd()
        assert "--resume" not in fresh


class TestUserFrameBuilder:
    def test_text_only_frame_remains_byte_for_byte_compatible(self):
        from subagents.cc_stream import build_user_frame

        payload = {
            "type": "user",
            "message": {"role": "user", "content": "plain text"},
        }

        assert build_user_frame("plain text") == (
            json.dumps(payload) + "\n"
        ).encode()

    def test_images_are_ordered_typed_sources_followed_by_exact_text(self):
        from subagents.cc_stream import build_user_frame

        first = b"first-image"
        second = b"second-image"
        projection = _media_projection(
            ("first", "image/png", first),
            ("second", "image/jpeg", second),
        )

        payload = json.loads(build_user_frame("compare exactly", projection))

        assert payload == {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.b64encode(first).decode("ascii"),
                        },
                    },
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": base64.b64encode(second).decode("ascii"),
                        },
                    },
                    {"type": "text", "text": "compare exactly"},
                ],
            },
        }
        rendered = build_user_frame("compare exactly", projection).decode()
        assert "data:image/" not in rendered
        assert "file_id" not in rendered
        assert "filename" not in rendered
        assert "Attached files" not in rendered

    @pytest.mark.asyncio
    async def test_first_and_continuation_writes_use_typed_frames_without_retention(self):
        from subagents.cc_stream import CCStreamSession

        writes: list[bytes] = []

        class Stdin:
            def write(self, payload):
                writes.append(payload)

            async def drain(self):
                return None

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="typed-images")
        session._proc = process
        initial = _media_projection(("initial", "image/png", b"initial"))
        continuation = _media_projection(
            ("follow-up", "image/webp", b"continuation")
        )

        await session.feed("initial text", media_projection=initial)
        await session.feed("continuation text", media_projection=continuation)

        assert [
            json.loads(frame)["message"]["content"][-1]
            for frame in writes
        ] == [
            {"type": "text", "text": "initial text"},
            {"type": "text", "text": "continuation text"},
        ]
        assert [
            json.loads(frame)["message"]["content"][0]["source"]["media_type"]
            for frame in writes
        ] == ["image/png", "image/webp"]
        assert session.input_frame_log == [
            {"type": "user", "message": {"role": "user", "content": "initial text"}},
            {
                "type": "user",
                "message": {"role": "user", "content": "continuation text"},
            },
        ]
        assert base64.b64encode(b"initial").decode("ascii") not in str(
            session.input_frame_log
        )


class TestFeedAcceptance:
    @pytest.mark.asyncio
    async def test_cancelled_feed_does_not_wait_for_never_draining_pipe(self):
        from subagents.cc_stream import CCStreamSession

        drain_entered = asyncio.Event()
        release_drain = asyncio.Event()
        write_observed: list[str] = []
        accepted: list[str] = []

        class Stdin:
            def write(self, _payload):
                return None

            async def drain(self):
                drain_entered.set()
                await release_drain.wait()

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        task = asyncio.create_task(session.feed(
            "next",
            on_write_observed=lambda: write_observed.append("written"),
            on_accepted=lambda: accepted.append("accepted"),
        ))
        await asyncio.wait_for(drain_entered.wait(), timeout=0.2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        done, _pending = await asyncio.wait({task}, timeout=0.1)

        release_drain.set()
        if not task.done():
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task in done
        assert write_observed == ["written"]
        assert accepted == []

    @pytest.mark.asyncio
    async def test_cancelled_pending_feed_does_not_confirm_acceptance(self):
        from subagents.cc_stream import CCStreamSession

        drain_entered = asyncio.Event()
        release_drain = asyncio.Event()
        write_observed: list[str] = []
        accepted: list[str] = []

        class Stdin:
            def write(self, _payload):
                return None

            async def drain(self):
                drain_entered.set()
                await release_drain.wait()

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        task = asyncio.create_task(session.feed(
            "next",
            on_write_observed=lambda: write_observed.append("written"),
            on_accepted=lambda: accepted.append("accepted"),
        ))
        await asyncio.wait_for(drain_entered.wait(), timeout=0.2)
        assert write_observed == ["written"]
        assert accepted == []
        task.cancel()
        release_drain.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert write_observed == ["written"]
        assert accepted == []

    @pytest.mark.asyncio
    async def test_cancelled_feed_confirms_drain_that_won_race(self):
        from subagents.cc_stream import CCStreamSession

        accepted: list[str] = []
        feed_task: asyncio.Task | None = None

        class Stdin:
            def write(self, _payload):
                return None

            async def drain(self):
                assert feed_task is not None
                feed_task.cancel()

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        feed_task = asyncio.create_task(session.feed(
            "next",
            on_accepted=lambda: accepted.append("accepted"),
        ))
        with pytest.raises(asyncio.CancelledError):
            await feed_task

        assert accepted == ["accepted"]

    @pytest.mark.asyncio
    async def test_feed_confirms_acceptance_only_after_drain(self):
        from subagents.cc_stream import CCStreamSession

        drain_entered = asyncio.Event()
        release_drain = asyncio.Event()
        observations: list[str] = []

        class Stdin:
            def write(self, _payload):
                observations.append("write")

            async def drain(self):
                drain_entered.set()
                await release_drain.wait()
                observations.append("drain")

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        task = asyncio.create_task(session.feed(
            "next",
            on_write_observed=lambda: observations.append("observed"),
            on_accepted=lambda: observations.append("accepted"),
        ))
        await asyncio.wait_for(drain_entered.wait(), timeout=0.2)
        assert observations == ["write", "observed"]
        release_drain.set()
        await task

        assert observations == ["write", "observed", "drain", "accepted"]

    @pytest.mark.asyncio
    async def test_pre_write_failure_reports_neither_write_nor_acceptance(self):
        from subagents.cc_stream import CCStreamSession

        observations: list[str] = []

        class Stdin:
            def write(self, _payload):
                raise BrokenPipeError("provider pipe is closed")

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        with pytest.raises(BrokenPipeError, match="provider pipe is closed"):
            await session.feed(
                "next",
                on_write_observed=lambda: observations.append("observed"),
                on_accepted=lambda: observations.append("accepted"),
            )

        assert observations == []

    @pytest.mark.asyncio
    async def test_drain_failure_reports_write_without_acceptance(self):
        from subagents.cc_stream import CCStreamSession

        observations: list[str] = []

        class Stdin:
            def write(self, _payload):
                observations.append("write")

            async def drain(self):
                raise BrokenPipeError("provider did not accept frame")

        process = type("Process", (), {
            "stdin": Stdin(),
            "returncode": None,
        })()
        session = CCStreamSession(binary="claude", session_id="acceptance")
        session._proc = process

        with pytest.raises(BrokenPipeError, match="provider did not accept frame"):
            await session.feed(
                "next",
                on_write_observed=lambda: observations.append("observed"),
                on_accepted=lambda: observations.append("accepted"),
            )

        assert observations == ["write", "observed"]



_FAKE_CLAUDE = textwrap.dedent(
    '''\
    #!{python}
    """Fake `claude -p` stream-json binary for tests.

    Ignores its CLI flags, emits an init event on start, and for every
    user frame read from stdin echoes an assistant text + a result event.
    Exits on stdin EOF — exercises the kept-open-pipe + multi-turn loop.
    """
    import sys, json

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    emit({{"type": "system", "subtype": "init", "session_id": "fake-sess",
           "model": "fake-model", "tools": [], "mcp_servers": [], "cwd": "."}})

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("type") == "control_request":
            request = msg.get("request", {{}})
            if request.get("subtype") == "set_model":
                emit({{"type": "control_response", "response": {{
                    "request_id": msg.get("request_id"), "subtype": "success",
                    "response": {{"model": request.get("model")}},
                }}}})
            continue
        content = msg.get("message", {{}}).get("content", "")
        if isinstance(content, list):
            content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
        emit({{"type": "assistant",
               "message": {{"content": [{{"type": "text", "text": "echo:" + content}}]}}}})
        emit({{"type": "result", "subtype": "success", "is_error": False,
               "result": "echo:" + content, "total_cost_usd": 0.0,
               "num_turns": 1, "stop_reason": "end_turn"}})
    '''
)


@pytest.fixture
def fake_claude(tmp_path):
    path = tmp_path / "fake_claude"
    path.write_text(_FAKE_CLAUDE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class TestStreamSessionRoundtrip:
    @pytest.mark.asyncio
    async def test_multi_turn_kept_open_pipe(self, fake_claude):
        from subagents.cc_stream import CCStreamSession

        events: list[dict] = []

        async def on_event(ev):
            events.append(ev)

        sess = CCStreamSession(binary=fake_claude, session_id="sess-1", on_event=on_event)
        await sess.start()
        await sess.feed("hello")
        await sess.feed("world")
        await sess.close(timeout=15)

        kinds = [e["kind"] for e in events]
        assert "init" in kinds
        init = next(e for e in events if e["kind"] == "init")
        assert init["session_id"] == "fake-sess"

        texts = [e["text"] for e in events if e["kind"] == "assistant_step"]
        assert "echo:hello" in texts
        assert "echo:world" in texts
        assert kinds.count("result") == 2
        assert sess.input_frame_log == [
            {"type": "user", "message": {"role": "user", "content": "hello"}},
            {"type": "user", "message": {"role": "user", "content": "world"}},
        ]

    @pytest.mark.asyncio
    async def test_set_model_keeps_the_open_session(self, fake_claude):
        from subagents.cc_stream import CCStreamSession

        sess = CCStreamSession(
            binary=fake_claude,
            session_id="s",
            model="opus",
        )
        await sess.start()
        await sess.set_model("sonnet")
        await sess.feed("still here")
        await sess.close(timeout=15)

        assert sess.model == "sonnet"
        assert sess.input_frame_log == [
            {"type": "user", "message": {"role": "user", "content": "still here"}},
        ]

    @pytest.mark.asyncio
    async def test_exits_cleanly_on_close(self, fake_claude):
        from subagents.cc_stream import CCStreamSession

        sess = CCStreamSession(binary=fake_claude, session_id="s", on_event=None)
        await sess.start()
        await sess.feed("ping")
        await sess.close(timeout=15)
        assert sess.returncode == 0



_BIG_LINE_CLAUDE = textwrap.dedent(
    '''\
    #!{python}
    """Fake `claude` that emits one HUGE single-line event (over asyncio's
    64 KiB default readline limit) before the normal echo + result."""
    import sys, json

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    emit({{"type": "system", "subtype": "init", "session_id": "fake-sess",
           "model": "fake-model", "tools": [], "mcp_servers": [], "cwd": "."}})

    line = sys.stdin.readline()
    emit({{"type": "user", "message": {{"content": [{{
        "type": "tool_result", "tool_use_id": "t1",
        "content": [{{"type": "text", "text": "x" * 200000}}]}}]}}}})
    emit({{"type": "assistant",
           "message": {{"content": [{{"type": "text", "text": "after-big"}}]}}}})
    emit({{"type": "result", "subtype": "success", "is_error": False,
           "result": "ok", "total_cost_usd": 0.0, "num_turns": 1,
           "stop_reason": "end_turn"}})
    '''
)


def _write_fake(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class TestReaderRobustness:
    @pytest.mark.asyncio
    async def test_line_over_64k_default_survives(self, tmp_path):
        from subagents.cc_stream import CCStreamSession

        binary = _write_fake(tmp_path, "big_line_claude", _BIG_LINE_CLAUDE)
        events: list[dict] = []

        async def on_event(ev):
            events.append(ev)

        sess = CCStreamSession(binary=binary, session_id="s", on_event=on_event)
        await sess.start()
        await sess.feed("go")
        await sess.close(timeout=15)

        kinds = [e["kind"] for e in events]
        assert "tool_result" in kinds, f"big tool_result lost; got {kinds}"
        big = next(e for e in events if e["kind"] == "tool_result")
        assert len(big["content"]) == 200000
        assert "after-big" in [e.get("text") for e in events if e["kind"] == "assistant_step"]
        assert "result" in kinds

    @pytest.mark.asyncio
    async def test_line_over_hard_limit_dropped_not_fatal(self, tmp_path, monkeypatch):
        import subagents.cc_stream as cs

        monkeypatch.setattr(cs, "STDOUT_LIMIT", 4096)
        binary = _write_fake(tmp_path, "big_line_claude", _BIG_LINE_CLAUDE)
        events: list[dict] = []

        async def on_event(ev):
            events.append(ev)

        sess = cs.CCStreamSession(binary=binary, session_id="s", on_event=on_event)
        await sess.start()
        await sess.feed("go")
        await sess.close(timeout=15)

        kinds = [e["kind"] for e in events]
        assert "tool_result" not in kinds  # the oversized event was dropped...
        assert "after-big" in [e.get("text") for e in events if e["kind"] == "assistant_step"]
        assert "result" in kinds  # ...but the stream resynced and finished

    @pytest.mark.asyncio
    async def test_reader_crash_fires_on_exit_loudly(self, fake_claude):
        import asyncio

        from subagents.cc_stream import CCStreamSession

        exits: list[tuple[int | None, str]] = []
        exited = asyncio.Event()

        async def on_event(ev):
            raise RuntimeError("boom in handler")

        async def on_exit(code, stderr):
            exits.append((code, stderr))
            exited.set()

        sess = CCStreamSession(binary=fake_claude, session_id="s",
                               on_event=on_event, on_exit=on_exit)
        await sess.start()
        await asyncio.wait_for(exited.wait(), timeout=15)

        assert len(exits) == 1
        code, stderr = exits[0]
        assert "CORSIA CC bridge error" in stderr
        assert "boom in handler" in stderr
        assert sess.returncode is not None  # process was killed, not leaked

    @pytest.mark.asyncio
    async def test_reader_crash_from_sqlite_names_the_error_code(self, fake_claude):
        import asyncio
        import sqlite3

        from subagents.cc_stream import CCStreamSession

        refusal = sqlite3.OperationalError("database is locked")
        refusal.sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"  # what a real refusal carries
        exits: list[str] = []
        exited = asyncio.Event()

        async def on_event(ev):
            raise refusal

        async def on_exit(code, stderr):
            exits.append(stderr)
            exited.set()

        sess = CCStreamSession(binary=fake_claude, session_id="s",
                               on_event=on_event, on_exit=on_exit)
        await sess.start()
        await asyncio.wait_for(exited.wait(), timeout=15)

        assert "CORSIA CC bridge error: OperationalError: database is locked [SQLITE_BUSY_SNAPSHOT]" in exits[0]


class TestExitCausality:
    @pytest.mark.asyncio
    async def test_terminal_successor_runs_after_terminal_callback_returns(
        self, monkeypatch,
    ):
        import importlib
        import core.turn_core as turn_core
        from subagents.cc_stream import CCStreamSession

        # This lazily loaded consumer imports the driver by value. Load it
        # before patching the driver so later tests cannot retain our fake.
        importlib.import_module("subagents.cc_process_exit")
        settlement = object()
        driven = asyncio.Event()
        session = None

        async def on_event(event):
            return settlement

        async def drive(received):
            assert received is settlement
            assert session is not None
            assert session._terminal_event is not None
            assert session._terminal_event.done()
            driven.set()

        monkeypatch.setattr(turn_core, "drive_turn_settlement", drive)
        session = CCStreamSession(
            binary="claude", session_id="terminal-order", on_event=on_event,
        )

        await session._dispatch_raw_event({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "done",
            "stop_reason": "end_turn",
        })

        await asyncio.wait_for(driven.wait(), timeout=1)
        assert session._terminal_driver is not None
        await session._terminal_driver

    def test_termination_request_surfaces_unexpected_kill_failure(self):
        from subagents.cc_stream import CCStreamSession

        class _Proc:
            returncode = None

            def kill(self) -> None:
                raise PermissionError("kill denied")

        session = CCStreamSession(binary="claude", session_id="kill-denied")
        session._proc = _Proc()

        with pytest.raises(PermissionError, match="kill denied"):
            session._request_terminate()

    @pytest.mark.asyncio
    async def test_termination_requested_before_spawn_prevents_process_start(
        self,
        monkeypatch,
    ):
        from subagents import cc_stream

        spawned = []

        async def spawn(*_args, **_kwargs):
            spawned.append(True)
            raise AssertionError("a pre-terminated CC session spawned a process")

        session = cc_stream.CCStreamSession(
            binary="claude", session_id="terminated-before-spawn",
        )
        session._request_terminate()
        monkeypatch.setattr(cc_stream.asyncio, "create_subprocess_exec", spawn)

        with pytest.raises(RuntimeError, match="terminated before process spawn"):
            await session.start()
        assert session._closing is True
        assert spawned == []
        assert session._proc is None
        assert session.input_frame_log == []

    @pytest.mark.asyncio
    async def test_termination_requested_during_spawn_reaps_before_feed(
        self,
        monkeypatch,
    ):
        from subagents import cc_stream

        spawn_entered = asyncio.Event()
        release_spawn = asyncio.Event()

        class _Proc:
            def __init__(self) -> None:
                self.returncode: int | None = None
                self.killed = False

            def kill(self) -> None:
                self.killed = True
                self.returncode = -9

            async def wait(self) -> int:
                assert self.returncode is not None
                return self.returncode

        proc = _Proc()

        async def spawn(*_args, **_kwargs):
            spawn_entered.set()
            await release_spawn.wait()
            return proc

        session = cc_stream.CCStreamSession(
            binary="claude", session_id="terminated-during-spawn",
        )
        monkeypatch.setattr(cc_stream.asyncio, "create_subprocess_exec", spawn)
        start_task = asyncio.create_task(session.start())
        await asyncio.wait_for(spawn_entered.wait(), timeout=0.5)
        session._request_terminate()
        release_spawn.set()

        with pytest.raises(RuntimeError, match="terminated during process spawn"):
            await asyncio.wait_for(start_task, timeout=0.5)
        assert session._closing is True
        assert session._proc is proc
        assert proc.killed is True
        assert session._reader is None
        assert session.input_frame_log == []

    @pytest.mark.asyncio
    async def test_exit_race_still_drains_reader_and_reaps(self):
        from subagents.cc_stream import CCStreamSession

        release_reader = asyncio.Event()
        reader_drained = asyncio.Event()

        class _Proc:
            returncode = None

            def kill(self) -> None:
                raise ProcessLookupError

            async def wait(self) -> int:
                self.returncode = 0
                return 0

        async def reader() -> None:
            await release_reader.wait()
            reader_drained.set()

        session = CCStreamSession(binary="claude", session_id="already-exited")
        session._proc = _Proc()
        session._reader = asyncio.create_task(reader())
        terminate_task = asyncio.create_task(session.terminate())
        await asyncio.sleep(0)
        assert session._closing is True
        assert terminate_task.done() is False

        release_reader.set()
        await terminate_task
        assert reader_drained.is_set()
        assert session.returncode == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("method", "expected_exits"),
        [("close", [(7, "natural failure detail")]), ("terminate", [])],
    )
    async def test_already_dead_process_respects_destructive_owner(
        self, method, expected_exits,
    ):
        from subagents.cc_stream import CCStreamSession

        stdout_started = asyncio.Event()
        release_stdout = asyncio.Event()
        exits: list[tuple[int | None, str]] = []

        class _Stdout:
            async def readline(self) -> bytes:
                stdout_started.set()
                await release_stdout.wait()
                return b""

        class _Stderr:
            async def read(self) -> bytes:
                return b"natural failure detail"

        class _Stdin:
            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                return None

        class _Proc:
            def __init__(self) -> None:
                self.stdout = _Stdout()
                self.stderr = _Stderr()
                self.stdin = _Stdin()
                self.returncode = 7

            def kill(self) -> None:
                raise AssertionError("already-dead process must not be killed")

            async def wait(self) -> int:
                return self.returncode

        async def on_exit(code: int | None, stderr: str) -> None:
            exits.append((code, stderr))

        session = CCStreamSession(
            binary="claude", session_id="already-dead", on_exit=on_exit,
        )
        session._proc = _Proc()
        session._reader = asyncio.create_task(session._read_loop())
        await stdout_started.wait()

        destructive = asyncio.create_task(getattr(session, method)())
        await asyncio.sleep(0)
        release_stdout.set()
        await destructive

        assert exits == expected_exits

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["close", "terminate"])
    async def test_live_destructive_call_suppresses_late_natural_callback(
        self, method,
    ):
        from subagents.cc_stream import CCStreamSession

        stderr_started = asyncio.Event()
        release_stderr = asyncio.Event()
        exits: list[tuple[int | None, str]] = []

        class _Stdout:
            async def readline(self) -> bytes:
                return b""

        class _Stderr:
            async def read(self) -> bytes:
                stderr_started.set()
                await release_stderr.wait()
                return b"destructive close detail"

        class _Stdin:
            def __init__(self, proc) -> None:
                self._proc = proc
                self._closing = False

            def is_closing(self) -> bool:
                return self._closing

            def close(self) -> None:
                self._closing = True
                self._proc.returncode = 0
                release_stderr.set()

        class _Proc:
            def __init__(self) -> None:
                self.stdout = _Stdout()
                self.stderr = _Stderr()
                self.returncode: int | None = None
                self.stdin = _Stdin(self)

            def kill(self) -> None:
                self.returncode = -9
                release_stderr.set()

            async def wait(self) -> int:
                assert self.returncode is not None
                return self.returncode

        async def on_exit(code: int | None, stderr: str) -> None:
            exits.append((code, stderr))

        session = CCStreamSession(
            binary="claude", session_id="explicit-close", on_exit=on_exit,
        )
        session._proc = _Proc()
        session._reader = asyncio.create_task(session._read_loop())
        await stderr_started.wait()

        await getattr(session, method)()

        assert exits == []
