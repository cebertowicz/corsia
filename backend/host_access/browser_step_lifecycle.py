"""Own step decisions, plan effects and monitoring; browser_control alone owns read guards."""
from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field, replace
from typing import Literal, Mapping

from host_access import browser_pane_state
from host_access.browser_cdp import PasswordProbe
from host_access.browser_steps import DONE_STATES, BrowserStep, PasswordStep, PersonStep
from infra.database import get_db
from infra.product_events import emit_product_event

log = logging.getLogger(__name__)
REPROBE_AFTER_INPUT = 0.5
REPROBE_EVERY = 2.0
Kind = Literal["password", "person"]


@dataclass(frozen=True)
class Answer:
    count: int
    declined: bool = False


@dataclass(frozen=True)
class Event:
    name: str
    result: PasswordProbe | None = None
    session: str | None = None
    source: str = "agent"
    instruction: str = ""


@dataclass(frozen=True)
class StepState:
    open: BrowserStep | None = None
    answers: Mapping[tuple[Kind, str], Answer] = field(default_factory=dict)
    row_state: str | None = None
    session: str | None = None


@dataclass(frozen=True)
class Decision:
    rules: tuple[str, ...] = ()
    end: str | None = None
    clear_answers: bool = False
    answer: Answer | None = None
    open_kind: Kind | None = None
    occurrence: int = 0
    refusal: str | None = None
    guard_present: bool = False
    monitor: bool | None = None


def _kind(step: BrowserStep) -> Kind:
    return "password" if isinstance(step, PasswordStep) else "person"


def _session_changed(state: StepState, event: Event) -> bool:
    return event.session is not None and (
        state.session is not None and event.session != state.session
        or state.open is not None and event.session != state.open.session
    )


def decide(state: StepState, event: Event) -> Decision:
    """R1–R13 in precedence order. No writes, clock, task scheduling or plan reads."""
    step, result = state.open, event.result
    if event.name == "session_ended":  # R1: no page observation can prove completion across sessions.
        return Decision(("R1",), end="unsettled" if step else None, clear_answers=True)
    if event.name in {"pane_probe_available", "pane_probe_gone"}:
        return Decision(monitor=event.name == "pane_probe_available")
    if _session_changed(state, event):  # R2 also clears answers when the prior session had no open step.
        following = decide(StepState(session=event.session), event)
        return replace(following, rules=("R2",) + following.rules,
                       end="unsettled" if step else None, clear_answers=True)
    end, rule = None, None
    if step is not None:
        if state.row_state is not None:
            end, rule = ("done", "R3") if state.row_state in DONE_STATES else ("declined", "R4")
        elif result is not None and result.target == step.target:
            if result.visit != step.visit:
                end, rule = "settle", "R5"
            elif isinstance(step, PasswordStep) and not result.visible_nodes and event.name != "declare":
                end, rule = "settle", "R6"
        if end is not None:
            assert rule is not None
            answer = Answer(step.occurrence, end == "declined")
            answers = dict(state.answers)
            answers[(_kind(step), step.visit)] = answer
            following = decide(StepState(answers=answers, session=state.session), event)
            same_person_visit = isinstance(step, PersonStep) and result is not None and result.visit == step.visit
            return replace(following, rules=(rule,) + following.rules, end=end, answer=answer,
                           guard_present=same_person_visit)
        if event.name == "declare" and isinstance(step, PersonStep) and result is not None and result.visit == step.visit:
            return Decision(("R12",))
        return Decision(("R13", "R7") if event.name == "declare" else ("R7",),
                        refusal="password" if isinstance(step, PasswordStep) else "person")
    if result is None:
        return Decision()
    if result.visible_nodes:
        answer = state.answers.get(("password", result.visit), Answer(0))
        if answer.declined:
            return Decision(("R8",), refusal="password_declined")
        return Decision(("R9",), open_kind="password", occurrence=answer.count + 1, refusal="password")
    if event.name == "declare":
        answer = state.answers.get(("person", result.visit), Answer(0))
        if answer.declined:
            return Decision(("R10",), refusal="person_declined")
        return Decision(("R11",), open_kind="person", occurrence=answer.count + 1)
    return Decision()


_steps: dict[str, BrowserStep] = {}
_answers: dict[str, dict[tuple[Kind, str], Answer]] = {}
_sessions: dict[str, str | None] = {}
_probe_locks: dict[str, asyncio.Lock] = {}


async def step_event(thread_id: str, event: Event, *,
                     observer: browser_pane_state.Observer | None = None) -> Decision:
    """Decide under the transition lock; notify the caller once before applying lifecycle effects."""
    async with _probe_locks.setdefault(thread_id, asyncio.Lock()):
        step = _steps.get(thread_id)
        current_state = StepState(step, _answers.get(thread_id, {}), session=_sessions.get(thread_id))
        # R1/R2 never use a row answer; teardown and replacement must not depend on plans.
        if (step is not None and event.name not in {"session_ended", "pane_probe_available", "pane_probe_gone"}
                and not _session_changed(current_state, event)):
            current_state = replace(current_state, row_state=await asyncio.to_thread(_step_row_state, step.node_id))
        decision = decide(current_state, event)
        result = event.result
        if observer is not None:
            observer(result, decision.clear_answers, decision.guard_present)
        if decision.clear_answers:
            _answers.pop(thread_id, None)
        if event.session is not None:
            _sessions[thread_id] = event.session
        if decision.end is not None and step is not None:
            if decision.end == "settle":
                await _settle_step(thread_id, step)
            _stop_reprobe(step)
            _steps.pop(thread_id, None)
            step.ended(thread_id)
            if decision.answer is not None:
                _answers.setdefault(thread_id, {})[(_kind(step), step.visit)] = decision.answer
            if decision.end == "declined":
                await _emit(f"{step.event_prefix}_declined", thread_id)
        if decision.open_kind is not None:
            assert result is not None
            if decision.open_kind == "password":
                opened: BrowserStep = PasswordStep(result.visit, result.host, result.target,
                                                  session=event.session, occurrence=decision.occurrence)
            else:
                opened = PersonStep(result.visit, result.host, result.target, instruction=event.instruction,
                                    session=event.session, occurrence=decision.occurrence)
            await _open_step(thread_id, opened)
        if (current := _steps.get(thread_id)) is not None:
            if decision.monitor is False:
                _stop_reprobe(current)
            elif decision.monitor is True and (current.reprobe is None or current.reprobe.done()):
                _start_reprobe(thread_id, current)
            if isinstance(current, PasswordStep) and decision.refusal == "password" and result is not None and result.visible_nodes:
                current.shown(thread_id)
        if event.name == "session_ended":
            _sessions.pop(thread_id, None)
        return decision


def discard_session(thread_id: str) -> None:
    """Forget in-memory ownership after a failed end hook, without any plan access."""
    if (step := _steps.pop(thread_id, None)) is not None:
        _stop_reprobe(step)
    _answers.pop(thread_id, None)
    _sessions.pop(thread_id, None)


def pane_event(thread_id: str, name: str) -> None:
    """Bridge synchronous pane-presence callbacks to the serialized transition executor."""
    task = asyncio.create_task(step_event(thread_id, Event(name)))
    task.add_done_callback(_pane_event_done)


def _pane_event_done(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.error("browser pane lifecycle event failed", exc_info=task.exception())


def person_input(thread_id: str) -> None:
    """Re-probe scheduling input, with no page or plan transition."""
    if (step := _steps.get(thread_id)) is not None and step.reprobe is not None:
        step.last_input = asyncio.get_running_loop().time()
        step.input_seen.set()


def _start_reprobe(thread_id: str, step: BrowserStep) -> None:
    if browser_pane_state.pane_probes(thread_id) and (step.reprobe is None or step.reprobe.done()):
        step.reprobe = asyncio.create_task(_reprobe_loop(thread_id, step))


def _stop_reprobe(step: BrowserStep) -> None:
    task, step.reprobe = step.reprobe, None
    if task is not None and task is not asyncio.current_task():
        task.cancel()


async def _reprobe_loop(thread_id: str, step: BrowserStep) -> None:
    """Re-probe after input pauses and at least every REPROBE_EVERY seconds (AR6)."""
    clock = asyncio.get_running_loop().time
    while True:
        next_probe = clock() + REPROBE_EVERY
        while True:
            step.input_seen.clear()
            due = next_probe if step.last_input is None else min(next_probe, step.last_input + REPROBE_AFTER_INPUT)
            try:
                await asyncio.wait_for(step.input_seen.wait(), max(0.0, due - clock()))
            except TimeoutError:
                break
        step.last_input = None
        try:
            if not await _reprobe(thread_id, step):
                return
        except Exception:
            log.warning("browser-step transition for %s failed; retrying on the next re-probe", thread_id, exc_info=True)


async def _reprobe(thread_id: str, step: BrowserStep) -> bool:
    """Deliver only this step's own target observations through the same transition."""
    probes = browser_pane_state.pane_probes(thread_id)
    current = _steps.get(thread_id)
    if current is None or current is not step or not probes:
        return False
    source = probes[0]
    try:
        result = await source.probe({})
    except Exception as exc:
        log.info("browser-step re-probe for %s failed: %s", thread_id, exc)
        result = None
    session = source.session() if source.session is not None else step.session
    if source.session is not None and session is None:
        await step_event(thread_id, Event("session_ended", session=step.session), observer=source.observer)
        return False
    if result is None or result.target != step.target:
        # Only this step's own page can supply an observation, including its end-time read guard.
        # A changed session still ends the old step unsettled even without a usable page result.
        if session == step.session:
            return True
        result = None
    await step_event(thread_id, Event("probe", result, session=session, source="pane"), observer=source.observer)
    return _steps.get(thread_id) is step and current.reprobe is not None


async def _open_step(thread_id: str, step: BrowserStep) -> None:
    """One receipt per kind, visit and occurrence; publish state only after the write succeeds."""
    base = f"{step.event_prefix}:{step.visit}"
    node_id = await asyncio.to_thread(
        _plan_write, "open_user_step", thread_id,
        key=base if step.occurrence == 1 else f"{base}:{step.occurrence}",
        title=step.title(), description=step.description(), provenance=step.provenance,
    )
    if not isinstance(node_id, str) or not node_id:
        raise RuntimeError("browser step row creation returned no node")
    step.node_id = node_id
    _steps[thread_id] = step
    _start_reprobe(thread_id, step)
    await _emit(f"{step.event_prefix}_opened", thread_id)


def _step_row_state(node_id: str | None) -> str | None:
    """The row's ended state, or None while it is still open."""
    if node_id is None:
        raise RuntimeError("browser step has no plan row")
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        node = public.node_row(db, node_id)
    finally:
        db.close()
    if node["state"] in public.TERMINAL_STATES:
        return str(node["state"])
    return "archived" if node["archived_at"] is not None else None


async def _settle_step(thread_id: str, step: BrowserStep) -> None:
    if step.node_id is None:
        return
    await asyncio.to_thread(
        _plan_write, "settle_user_step", step.node_id,
        evidence=step.settlement_evidence, provenance=step.provenance,
    )
    await _emit(f"{step.event_prefix}_settled", thread_id)


def _plan_write(operation: str, *args: object, **kwargs: object) -> str | None:
    """Use plans.public in a dedicated off-loop connection."""
    public = importlib.import_module("plans.public")
    db = get_db()
    try:
        return getattr(public, operation)(db, *args, **kwargs)
    finally:
        db.close()


async def _emit(action: str, thread_id: str) -> None:
    await asyncio.to_thread(emit_product_event, "browser_pane", action, thread_id=thread_id)


def has_step(thread_id: str) -> bool:
    return thread_id in _steps


def password_page(thread_id: str) -> bool:
    return isinstance(_steps.get(thread_id), PasswordStep)
