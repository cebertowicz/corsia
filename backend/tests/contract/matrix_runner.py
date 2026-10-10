"""Turn-lifecycle matrix driver: boot the isolated backend, drive real turns.

Phase 0 deliverable of the collapse plan — the executable half of the matrix
``{plain, tool, stop-mid-generation, retry, checkpoint-unavailable, queued-preemption, pressure,
restart-recovery} x {native, claude-code, codex}``, run against the ISOLATED
environment (``e2e_app`` + ``e2e_mock``) rather than mocks of our own code.
Every path is driven the way a user drives it: an HTTP session create, a
WebSocket that authenticates, subscribes and sends, and the event stream read
back frame by frame.

Why it exists: in the week of 2026-08-03..07 each of these lifecycle paths broke
in production while a 6900-test suite stayed green, because no test drove any of
them end to end. Four proven-broken paths are cells here: the first tool-using
turn on a thread, Stop during generation, a mid-turn pressure pause, and
restart-recovery leaving events undelivered.

The driver holds no assertions. It returns one ``CellResult`` per cell —
``covered``, ``uncovered`` with the precise reason, or ``failed`` with evidence.
A cell this harness cannot honestly drive is reported uncovered, and a covered
cell may still name a ``LIMIT:`` it did not reach; both print on every run,
because truncating coverage in silence is itself a contract violation (the
post-mortem's "no silent caps").
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
import websockets

from e2e_pressure_fixture import PRESSURE_MODEL_BY_HARNESS
from tests.contract.matrix_backend import BACKEND_ROOT, BackendUnavailable, IsolatedBackend
from auth_fixtures import OWNER_TOKEN

TOKEN = OWNER_TOKEN

HARNESSES = ("native", "claude-code", "codex")
MODEL_BY_HARNESS = {
    "native": "mock/test-model",
    "claude-code": "claude-code/fixture-cc",
    "codex": "codex/fixture-codex",
}
SCENARIOS = (
    "plain", "tool", "stop-mid-generation", "retry", "checkpoint-unavailable",
    "queued-preemption", "pressure", "restart-recovery",
)

# Shape the production checkpoint decoder accepts, mirroring the browser E2E's
# CHECKPOINT_REPLY (frontend/e2e/flows/chat-send-receive.spec.ts).
CHECKPOINT_REPLY = {
    "content": json.dumps({
        "schema_version": 1, "objective": "Continue the exact thread.",
        "status": "in_progress", "approvals_and_preferences": [],
        "active_constraints": [], "rejected_alternatives": [],
        "decisions": [], "relevant_state": [],
        "verification": [], "known_failures": [], "open_questions": [],
        "blockers": [], "next_actions": [], "recently_completed": [],
        "facts": [], "hypotheses": [],
    }),
    "finish_reason": "stop",
}


@dataclass
class CellResult:
    """One matrix cell's honest outcome."""

    scenario: str
    harness: str
    status: str  # "covered" | "uncovered" | "failed"
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def cell(self) -> str:
        """Human name of this cell, as the scoreboard spells it."""
        return f"{self.scenario} x {self.harness}"


class BackendClient:
    """HTTP surface of the isolated backend, spelled as the frontend spells it."""

    def __init__(self, backend: IsolatedBackend, harness: str = "native"):
        self.backend = backend
        self.harness = harness

    @property
    def model(self) -> str:
        """Normal catalog model used by this harness's matrix cells."""
        return MODEL_BY_HARNESS[self.harness]

    @property
    def pressure_model(self) -> str:
        """Small-window catalog model used to force this harness's hard wall."""
        return PRESSURE_MODEL_BY_HARNESS[self.harness]

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Issue one authenticated request and return its parsed JSON body."""
        async with httpx.AsyncClient(base_url=self.backend.base_url, timeout=60) as c:
            response = await c.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

    async def create_session(self, title: str, model: str | None = None) -> str:
        """Create a thread on the isolated mock model — one thread per cell."""
        body = await self._request(
            "POST", f"/api/threads?token={TOKEN}",
            json={"title": title, "model": model or self.model},
        )
        return str(body["thread_id"])

    async def switch_model(self, thread_id: str, model: str, **extra: Any) -> dict:
        """Switch a thread's model exactly as the picker does: model alone.

        The endpoint refuses a model patched alongside any other field, so a
        helper that bundles them would test a request the product cannot make.
        """
        return await self._request(
            "PATCH",
            f"/api/threads/{thread_id}?token={TOKEN}",
            json={"model": model, **extra},
        )

    async def session_row(self, thread_id: str) -> dict:
        """The thread's own durable row, as the client reads it back."""
        body = await self._request("GET", f"/api/threads?token={TOKEN}")
        for row in body["threads"]:
            if str(row["id"]) == thread_id:
                return dict(row)
        raise AssertionError(f"session {thread_id} is not in the list")

    async def seed_replies(self, replies: list[dict]) -> int:
        """Replace the mock provider's reply queue; returns the accepted count."""
        payload = {"replies": [{"role": "assistant", **r} for r in replies]}
        return int((await self._request(
            "POST", "/api/test/mock-responses", json=payload))["queued"])

    async def call_count(self) -> int:
        """Provider calls made since the last seed — a retry cascade shows up here."""
        return int((await self._request("GET", "/api/test/mock-responses"))["call_count"])

    async def seed_pressured_history(self, thread_id: str) -> int:
        """Seed history that forces the checkpoint gate; returns the model window."""
        result = await self._request(
            "POST", f"/api/test/seed-pressured-history?token={TOKEN}",
            json={"thread_id": thread_id},
        )
        assert result["model"] == self.pressure_model
        return int(result["context_window"])

    async def messages(self, thread_id: str) -> list[dict]:
        """Durable thread rows, including each input's delivery state."""
        body = await self._request(
            "GET", f"/api/threads/{thread_id}/messages?token={TOKEN}")
        return list(body["messages"])

    async def message_action(self, thread_id: str, message_id: str, action: str) -> dict:
        """Invoke a pause exit (``retry`` / ``cancel``) exactly as the bubble does."""
        return await self._request(
            "POST",
            f"/api/threads/{thread_id}/messages/{message_id}/{action}?token={TOKEN}",
        )

    async def release_gate(self, gate: str) -> None:
        """Let a gated generation finish, so a hold is a decision and not a race.

        ``delay_ms`` holds a turn for a wall-clock guess; a gate holds it until
        the test says otherwise. Journeys that act AT a boundary (queue during
        generation, Stop mid-tool, cancel while an older input is in flight)
        need the boundary to be exact, not likely.
        """
        await self._request("POST", f"/api/test/mock-gates/{gate}/release")

    async def context_preview(self, thread_id: str) -> dict:
        """The composer chip's own number: the next dispatch's context estimate."""
        body = await self._request(
            "GET",
            f"/api/threads/{thread_id}/context-snapshot/next-turn?token={TOKEN}",
        )
        return dict(body["preview"])

    async def active_turn_count(self, thread_id: str | None = None) -> int:
        """Turns the backend still considers live — the truth behind Stop.

        Scoped to one thread when asked, because that is the claim a composer
        makes: this thread has a turn to stop, or it does not. A fleet-wide
        count would fail a thread for a sibling thread's work.
        """
        body = await self._request("GET", f"/api/turns/active?token={TOKEN}")
        turns = list(body["active_turns"])
        if thread_id is None:
            return int(body["count"])
        return len([t for t in turns if t.get("thread_id") == thread_id])


class TurnDriver:
    """One authenticated WebSocket, driven the way the chat client drives it."""

    def __init__(self, backend: IsolatedBackend):
        self.backend = backend
        self._ws: Any = None
        self.frames: list[dict] = []

    async def __aenter__(self) -> "TurnDriver":
        """Connect, authenticate, and refuse to proceed on a rejected handshake."""
        self._ws = await websockets.connect(self.backend.ws_url, max_size=None)
        await self._ws.send(json.dumps({"token": TOKEN}))
        auth = json.loads(await asyncio.wait_for(self._ws.recv(), timeout=15))
        if auth.get("type") != "auth_ok":
            raise BackendUnavailable(f"WebSocket auth failed: {auth}")
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the socket; a cell that crashed the backend has none left to close."""
        try:
            await self._ws.close()
        except (websockets.WebSocketException, OSError):
            pass  # the peer is already gone — that is what a crash cell leaves

    async def subscribe(self, thread_id: str) -> list[dict]:
        """Join the session's broadcast list and wait for its snapshot reply."""
        frame = {"type": "subscribe_session", "thread_id": thread_id}
        await self._ws.send(json.dumps(frame))
        return await self.collect(
            thread_id, until=("session_snapshot",), timeout=15,
        )

    async def observe_recovery(
        self, thread_id: str, timeout: float,
    ) -> tuple[list[dict], str | None]:
        """Prove recovery is inactive or observe its terminal subscriber frame."""
        recovered = await self.subscribe(thread_id)
        snapshots = [
            frame for frame in recovered
            if frame.get("type") == "session_snapshot"
        ]
        if not snapshots:
            return recovered, "subscription never delivered an authoritative session snapshot"
        active_turn_id = snapshots[-1].get("turn_id")
        if active_turn_id is None:
            return recovered, None

        def observed_active_terminal() -> bool:
            return any(
                frame.get("type") in {"turn_complete", "error"}
                and frame.get("turn_id") == active_turn_id
                for frame in recovered
            )

        if observed_active_terminal():
            return recovered, None
        recovered += await self.collect(
            thread_id, until=("turn_complete", "error"), timeout=timeout,
        )
        if not observed_active_terminal():
            return recovered, "active recovery never reached a terminal subscriber frame"
        return recovered, None

    async def send(self, thread_id: str, content: str, **extra: Any) -> str:
        """Send one chat input; returns the ``input_id`` used to correlate it."""
        input_id = str(uuid.uuid4())
        await self._ws.send(json.dumps({
            "type": "message", "thread_id": thread_id,
            "content": content, "input_id": input_id, **extra,
        }))
        return input_id

    async def stop(self, thread_id: str) -> None:
        """Press Stop on this thread, exactly as the composer's button does."""
        await self._ws.send(json.dumps({"type": "stop", "thread_id": thread_id}))

    async def collect(
        self, thread_id: str, *, until: tuple[str, ...], timeout: float,
    ) -> list[dict]:
        """Read ``session_id`` frames until one of ``until`` arrives or time runs out."""
        end = time.monotonic() + timeout
        out: list[dict] = []
        while time.monotonic() < end:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except (websockets.WebSocketException, OSError) as closed:
                out.append({"type": "__socket_closed__", "detail": repr(closed)})
                return out
            frame = json.loads(raw)
            if frame.get("type") == "ping":
                continue
            if frame.get("thread_id") not in (None, thread_id):
                continue
            out.append(frame)
            self.frames.append(frame)
            if frame.get("type") in until:
                break
        return out


def frame_types(frames: list[dict]) -> list[str]:
    """Event-type spelling of a frame list, for evidence lines."""
    return [str(f.get("type")) for f in frames]


def assistant_texts(frames: list[dict]) -> list[str]:
    """Persisted assistant step texts — what the user would actually read."""
    return [str(f.get("content") or "") for f in frames if f.get("type") == "step_text"]


# A scenario drives one lifecycle path against ``BackendClient`` (``.backend``
# is the live process) and reports facts: the evidence it observed, plus a
# violation sentence when the contract was broken (else None). Scenarios never
# assert — judgment belongs to the contract test.
Scenario = Callable[[BackendClient], Awaitable[tuple[list[str], "str | None"]]]


async def run_matrix(scenarios: dict[str, Scenario], root: Path) -> list[CellResult]:
    """Boot one isolated backend and drive every cell of the matrix once.

    Cells run sequentially — the ``e2e_mock`` reply queue is process-global, so
    concurrent cells eat each other's replies — and each must leave no thread
    re-probing behind. ``restart-recovery`` crashes the shared process, so last.
    """
    backend = IsolatedBackend(root)
    backend.boot()
    results: list[CellResult] = []
    try:
        for name in SCENARIOS:
            for harness in HARNESSES:
                results.append(await _run_cell(
                    name, harness, scenarios, BackendClient(backend, harness),
                ))
    finally:
        backend.stop()
    return results


async def _run_cell(
    name: str, harness: str, scenarios: dict[str, Scenario], client: BackendClient,
) -> CellResult:
    """Drive one cell, converting a driver explosion into reviewable evidence."""
    scenario = scenarios.get(name)
    if scenario is None:
        return CellResult(name, harness, "uncovered", reason="no driver implemented")
    try:
        evidence, violation = await scenario(client)
    except BackendUnavailable:
        raise
    except Exception as exploded:  # noqa: BLE001 — a driver crash is the finding
        return CellResult(
            name, harness, "failed",
            reason=f"driver raised {type(exploded).__name__}: {exploded}",
            evidence=[client.backend.log_tail(8)],
        )
    status = "failed" if violation else "covered"
    return CellResult(name, harness, status, reason=violation or "", evidence=evidence)


def render_table(results: list[CellResult]) -> str:
    """Render the scoreboard: the grid, then failures, partials, and uncovered."""
    width = max(len(name) for name in SCENARIOS)
    by_cell = {(r.scenario, r.harness): r for r in results}
    mark = {"covered": "PASS", "uncovered": "UNCOVERED", "failed": "FAIL"}
    lines = [
        f"{'scenario'.ljust(width)} | " + " | ".join(h.ljust(11) for h in HARNESSES),
        "-" * (width + 3 + 14 * len(HARNESSES)),
    ]
    for scenario in SCENARIOS:
        cells = " | ".join(
            mark.get(getattr(by_cell.get((scenario, h)), "status", ""), "MISSING").ljust(11)
            for h in HARNESSES
        )
        lines.append(f"{scenario.ljust(width)} | {cells}")
    for result in [r for r in results if r.status == "failed"]:
        lines.append(f"\nFAIL  {result.cell}: {result.reason}")
        lines.extend(f"        {line}" for line in result.evidence)
    # A covered cell may still name what it did not reach. Those lines print on
    # every run, pass or fail — an unread limit is a silent cap.
    for result in [r for r in results if r.status == "covered"]:
        for limit in [e for e in result.evidence if e.startswith("LIMIT:")]:
            lines.append(f"\nPARTIAL  {result.cell} {limit}")
    for harness in HARNESSES:
        blocked = [r for r in results if r.status == "uncovered" and r.harness == harness]
        if blocked:
            lines.append(
                f"\nUNCOVERED  {len(blocked)}/{len(SCENARIOS)} {harness} cells "
                f"({', '.join(r.scenario for r in blocked)}): {blocked[0].reason}"
            )
    return "\n".join(lines)
