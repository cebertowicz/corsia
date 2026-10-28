"""Persistent Claude Code pipe: open stdin, ``feed()`` frames, NDJSON events.

Normalized events reach the trusted callback without loosening its sandbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from core._tool_result_delivery_support import ToolResultBindingError
from core.fork_cc_context import CCForkContext
from infra.write_admission import sqlite_errorname_tag
from subagents.cc_confinement import drop_review_credentials
from subagents.cc_invocation import CONFINEMENT_NONE, ClaudeInvocation, build_claude_cmd
from subagents.cc_env import apply_claude_config_dir, clean_claude_env
from subagents.cc_events import normalize_event
from subagents.cc_stream_frames import build_user_frame, read_raw_event
from subagents.cc_stream_control import (
    fail_pending as fail_pending_controls,
    interrupt as interrupt_stream,
    observe_terminal_event as observe_control_terminal_event,
    resolve_response as resolve_control_response,
    record_tool_reply as record_stream_tool_reply,
    set_model as set_stream_model,
)
from subagents.stream_capture import record_frame

if TYPE_CHECKING:
    from core.media_projection import ValidatedMediaProjection

log = logging.getLogger(__name__)

OnEvent = Callable[[dict], Awaitable[object | None]]
OnExit = Callable[[int | None, str], Awaitable[None]]

STDOUT_LIMIT = 16 * 1024 * 1024


class CCStreamSession:
    """Drives one ``claude -p`` stream-json process over a kept-open stdin pipe."""

    def __init__(
        self,
        *,
        binary: str,
        session_id: str,
        seed_file: str | None = None,
        mcp_config: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str = "bypassPermissions",
        working_dir: str | None = None,
        claude_dir: str | None = None,
        on_event: OnEvent | None = None,
        on_exit: OnExit | None = None,
        extra_args: list[str] | None = None,
        resume_session_id: str | None = None,
        corsia_thread_id: str | None = None,
        reconstructed_transcript_path: str | None = None,
        confinement: str = CONFINEMENT_NONE,
        add_dirs: tuple[str, ...] = (),
    ) -> None:
        self.transport_generation_id = str(uuid.uuid4())
        self.binary = binary
        self.session_id = session_id
        self.resume_session_id = resume_session_id
        self.corsia_thread_id = corsia_thread_id
        self.reconstructed_transcript_path = reconstructed_transcript_path
        self.seed_file = seed_file
        self.mcp_config = mcp_config
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.working_dir = working_dir
        self.claude_dir = claude_dir
        self.confinement = confinement
        self.add_dirs = add_dirs
        self._on_event = on_event
        self._on_exit = on_exit
        self._extra_args = extra_args or []
        self.input_frame_log: list[dict] = []
        self._closing = False
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._terminal_event: asyncio.Task[object | None] | None = None
        self._terminal_driver: asyncio.Task | None = None
        self._stderr: bytes = b""
        self._interrupt_seq = 0
        self._control_seq = 0
        self._control_responses: dict[str, asyncio.Future[dict]] = {}
        self.tool_context = CCForkContext(None, None)
        self._context_changed = asyncio.Event()

    def invocation(self) -> ClaudeInvocation:
        """This session's exact launch description, reusable by a fork of it."""
        return ClaudeInvocation(
            binary=self.binary,
            permission_mode=self.permission_mode,
            session_id=self.session_id,
            resume_session_id=self.resume_session_id,
            seed_file=self.seed_file,
            mcp_config=self.mcp_config,
            working_dir=self.working_dir,
            model=self.model,
            effort=self.effort,
            confinement=self.confinement,
            add_dirs=self.add_dirs,
        )

    def build_cmd(self) -> list[str]:
        """Assemble the ``claude -p`` stream-json invocation."""
        return build_claude_cmd(
            self.invocation(),
            stream_input=True,
            extra_args=self._extra_args,
        )

    def _build_env(self) -> dict[str, str]:
        """Build the spawned process environment."""
        env = clean_claude_env()
        env.pop("CORSIA_THREAD_ID", None)
        if self.corsia_thread_id:
            env["CORSIA_THREAD_ID"] = self.corsia_thread_id
        apply_claude_config_dir(env, self.claude_dir)
        env["CLAUDE_CODE_DISABLE_BUNDLED_SKILLS"] = "1"
        if self.confinement != CONFINEMENT_NONE:
            drop_review_credentials(env)
        return env

    async def start(self) -> None:
        """Spawn the process (stdin kept open) and launch the reader task."""
        if self._proc is not None:
            raise RuntimeError("CCStreamSession already started")
        if self._closing:
            raise RuntimeError("CCStreamSession terminated before process spawn")
        from infra.db_async import run_in_db
        from subagents.vendor_write_admission import prepare_session_launch

        await run_in_db(prepare_session_launch, self, 'claude-code')
        from subagents.cc_sessions import require_resume_in_root

        require_resume_in_root(self.resume_session_id, self.reconstructed_transcript_path, self.claude_dir)
        self._proc = await asyncio.create_subprocess_exec(
            *self.build_cmd(),
            cwd=self.working_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=STDOUT_LIMIT,
            env=self._build_env(),
        )
        if self._closing:
            await self.terminate()
            raise RuntimeError("CCStreamSession terminated during process spawn")
        self._reader = asyncio.create_task(self._read_loop())

    async def feed(
        self,
        text: str,
        *,
        media_projection: "ValidatedMediaProjection | None" = None,
        on_write_observed: Callable[[], None] | None = None,
        on_accepted: Callable[[], None] | None = None,
        input_context: tuple[str, int] | None = None,
        input_id: str | None = None,
    ) -> None:
        """Write one user frame to the kept-open stdin pipe (steer / reply-into)."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("CCStreamSession not started")
        if self._proc.returncode is not None:
            raise RuntimeError(
                f"feed to dead CC process (returncode={self._proc.returncode})"
            )
        frame = build_user_frame(text, media_projection, input_id=input_id or (input_context[0] if input_context else None))
        from subagents.vendor_write_admission import require_session_write

        require_session_write(self, "claude-code")
        self._proc.stdin.write(frame)
        if input_context is not None:
            self.tool_context.pending_inputs[input_context[0]] = input_context[1]
        del frame
        self.input_frame_log.append({"type": "user", "message": {"role": "user", "content": text}})
        if on_write_observed is not None:
            on_write_observed()
        from subagents.cc_write_confirmation import confirm_frame_drain

        await confirm_frame_drain(self._proc.stdin, on_accepted)

    async def interrupt(self) -> bool:
        return await interrupt_stream(self)

    async def set_model(self, model: str, *, timeout: float = 10.0) -> None:
        await set_stream_model(self, model, timeout=timeout)

    def _resolve_control_response(self, raw: dict) -> bool:
        return resolve_control_response(self, raw)

    def _fail_pending_controls(self, detail: str) -> None:
        fail_pending_controls(self, detail)

    async def _read_raw_event(self) -> tuple[bool, object | None]:
        return await read_raw_event(self, STDOUT_LIMIT)

    async def record_tool_reply(self, call_id: str, content: str, target) -> None:
        """Count one projected result against its complete current model call."""
        await record_stream_tool_reply(self, call_id, content, target)

    async def _dispatch_raw_event(self, raw: object) -> None:
        record_frame(getattr(self, "corsia_thread_id", None), "cc", raw)
        if isinstance(raw, dict):
            self.tool_context.observe(raw)
            self._context_changed.set()
        if isinstance(raw, dict) and raw.get("type") == "control_response":
            self._resolve_control_response(raw)
            return
        for event in normalize_event(raw):
            if (event.get("kind") == "final_call_usage" and self.tool_context.input_measured
                    and self.tool_context.output_measured):
                event = {**event, **self.tool_context.final_call_identity()}
            if self._on_event is None:
                continue
            if event.get("kind") == "result":
                previous = self._terminal_event
                task = asyncio.create_task(
                    self._deliver_terminal_event(previous, event),
                    name=f"cc-terminal:{self.corsia_thread_id}",
                )
                self._terminal_event = task
                task.add_done_callback(self._observe_terminal_event)
                continue
            await self._join_terminal_event()
            try:
                await self._on_event(event)
            except ToolResultBindingError:
                log.exception(
                    "cc_stream: tool result could not be bound on session %s; "
                    "skipping this event and keeping the session alive",
                    self.session_id,
                )

    async def _deliver_terminal_event(
        self,
        previous: asyncio.Task[object | None] | None,
        event: dict,
    ) -> object | None:
        if previous is not None:
            await asyncio.shield(previous)
        if self._on_event is not None:
            try:
                return await self._on_event(event)
            except Exception as error:
                from subagents.cc_process_exit import _CommittedTerminalFailure

                if isinstance(error, _CommittedTerminalFailure):
                    return error
                raise
        return None

    async def _join_terminal_event(self) -> None:
        task = self._terminal_event
        if task is not None and not task.done():
            await asyncio.shield(task)
        if task is not None and task.done():
            task.result()

    def _observe_terminal_event(self, task: asyncio.Task[object | None]) -> None:
        observe_control_terminal_event(self, task)

    async def _finish_reader(self, crash: str | None) -> None:
        self._context_changed.set()
        self._fail_pending_controls(
            "Claude Code exited before control acknowledgement"
        )
        try:
            await self._join_terminal_event()
        except Exception as exc:
            if crash is None:
                crash = f"terminal event failed: {type(exc).__name__}: {exc}{sqlite_errorname_tag(exc)}"
        if self._closing or self._on_exit is None:
            return
        assert self._proc is not None
        stderr = await self._read_stderr()
        try:
            await self._proc.wait()
        except (OSError, RuntimeError) as exc:  # pragma: no cover - defensive
            log.warning("cc_stream: wait() after exit failed: %s", exc)
        self._context_changed.set()
        if crash and not stderr.strip():
            stderr = f"CORSIA CC bridge error: {crash}"
        if stderr.strip():
            log.log(logging.ERROR if self._proc.returncode not in (0, None) else logging.WARNING, "cc_stream: claude exited %s: %s", self._proc.returncode, stderr[:500])
        if not self._closing:
            await self._on_exit(self._proc.returncode, stderr)

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        crash: str | None = None
        try:
            while True:
                ended, raw = await self._read_raw_event()
                if ended:
                    break
                if raw is not None:
                    await self._dispatch_raw_event(raw)
        except Exception as exc:
            crash = f"{type(exc).__name__}: {exc}{sqlite_errorname_tag(exc)}"
            log.exception("cc_stream: reader crashed; terminating claude")
            if self._proc.returncode is None:
                self._proc.kill()
        await self._finish_reader(crash)

    async def _read_stderr(self) -> str:
        """Drain stderr to EOF without raising into the read loop."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = await self._proc.stderr.read()
        except (OSError, ValueError) as e:
            log.debug("cc_stream: stderr read failed: %s", e)
            return ""
        return data.decode("utf-8", "replace")

    async def _drain_reader(self) -> None:
        reader = self._reader
        if reader is None:
            return
        result = await asyncio.shield(asyncio.gather(reader, return_exceptions=True))
        if isinstance(result[0], Exception):
            log.warning("cc_stream: drained reader exit failed: %s", result[0])

    async def close(self, timeout: float = 30.0) -> int | None:
        """Close stdin, drain remaining events, await exit. Returns the exit code."""
        if self._proc is None:
            return None
        if self._proc.returncode is None:
            self._closing = True
            self._context_changed.set()
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            self._proc.stdin.close()
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._drain_reader(), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("cc_stream: reader did not drain in %ss; killing", timeout)
                self._proc.kill()
                await self._drain_reader()
            if self._reader.cancelled() and self._proc.returncode is None:
                self._proc.kill()
        if self._proc.stderr is not None:
            self._stderr = await self._proc.stderr.read()
        await self._proc.wait()
        if self._stderr.strip():
            msg = self._stderr.decode("utf-8", "replace")[:500]
            if self._proc.returncode not in (0, None):
                log.error("cc_stream: claude exited %s: %s", self._proc.returncode, msg)
            else:
                log.warning("cc_stream: claude stderr (exit ok): %s", msg)
        return self._proc.returncode

    def _request_terminate(self) -> None:
        # Destructive ownership is durable even before the spawn await returns.
        self._closing = True
        self._context_changed.set()
        if self._proc is None:
            return
        # Destructive ownership closes a not-yet-delivered natural-exit
        # callback even when the OS has already published the return code.
        if self._proc.returncode is not None:
            return
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass

    async def terminate(self) -> None:
        """Force-kill and drain so no buffered output can land after return."""
        self._request_terminate()
        if self._proc is None:
            return
        try:
            await self._drain_reader()
        except asyncio.CancelledError:
            await self._drain_reader()
            raise
        finally:
            await self._proc.wait()

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc is not None else None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None
