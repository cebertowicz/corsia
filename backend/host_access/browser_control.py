"""Own page access and password read guards; browser_step_lifecycle owns step transitions."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import partial
from typing import Awaitable, Callable

from host_access import browser_pane_state, browser_step_lifecycle
from host_access.browser_cdp import PasswordProbe
from host_access.browser_session_state import BrowserSessionError
from host_access.browser_url import is_http_url as is_http_url, normalize_browser_url as normalize_browser_url
from infra import state

log = logging.getLogger(__name__)

PASSWORD_PAGE = (
    "This page asks for a password, so it is the person's to complete. They already see a notice on the "
    "page and a task in their attention list, and you get a turn once they finish or decline it; end your "
    "turn. If they decline, tell them in chat what you needed the page for and stop."
)
# A read guard outlives its step, so its refusal promises nothing about a notice, a task or a turn.
PASSWORD_READ_REFUSED = (
    "This page has shown a password field, so running scripts on it and reading that field stay refused. "
    "Other commands still work."
)
PAGE_CHECK_FAILED = "This page couldn't be checked"
# A failed plan write loses at most the person's task for the page, never the browser itself. What became of
# the row is unknown (a failed read or emit leaves it in place), so the wording claims neither state.
STEP_NOT_RECORDED = (
    "Note: the sign-in task for this page may not have been recorded. If you need the person to sign in and "
    "haven't told them, say so in chat."
)
DECLARATION_NOT_RECORDED = (
    "The person step may not have been recorded, so the person may have no task for this page. "
    "Tell them in chat what you need from the page and stop."
)
# A visible password field is the person's to fill in; with no task recorded for it, the command is refused.
PASSWORD_PAGE_UNRECORDED = (
    "This page is waiting for the person to sign in, and their sign-in task couldn't be recorded. "
    "Ask them in chat to sign in in the shared browser, then try again."
)
PERSON_STEP = (
    "This browser step is the person's to complete. They already see a task in their attention list, and you "
    "get a turn once they finish or decline it; end your turn. If they decline, tell them in chat what you "
    "needed the page for and stop."
)
PASSWORD_DECLINED = (
    "The person declined to enter a password on this page. Tell them in chat what you needed the page for and stop."
)
PERSON_STEP_DECLINED = (
    "The person declined this step on this page, so it can't be given to them again. "
    "Tell them in chat what you needed the page for and stop."
)
FILLED_FIELD_REFUSED = (
    "The person filled in a password field on this page, so until you open another page or reload this one, "
    "only commands that can't show what they typed run here: open, back, forward, reload, click, dblclick, "
    "hover, focus, fill, type, press, select, check, uncheck, scroll, scrollintoview, "
    "wait (but not --fn or --download), tab, snapshot (field values hidden), "
    "get (but not get html, get value or a value attribute), and is."
)
FILLED_FIELD_SNAPSHOT_WITHHELD = (
    "Error: this snapshot was withheld. The person filled in a password field on this page, and "
    "agent-browser's output wasn't a snapshot whose field values could be hidden. Run snapshot again."
)
# Once the person marks a password field done (R14), any field on that visit may display what they typed: a
# text-type field, a show-password toggle, or one that swaps the field for a new text input the probe can't
# tell from any other. So for the rest of the visit only commands that never render a field's value or run
# a page script run, even after the field is gone.
_FILLED_FIELD_COMMANDS = frozenset({
    "open", "back", "forward", "reload", "click", "dblclick", "hover", "focus", "fill", "type", "press",
    "select", "check", "uncheck", "scroll", "scrollintoview", "tab", "is",
    # Only session teardown checks with close (an agent's own close is refused before any page check).
    "close",
})
_FILLED_FIELD_GETS = frozenset({"title", "url", "text", "count", "attr", "box", "styles"})
# wait's options that take one operand and run no script; --fn evaluates one and --download writes a file.
_FILLED_FIELD_WAIT_OPTIONS = frozenset({"--load", "--url", "--text", "--state"})
_FILLED_FIELD_SNAPSHOTS = frozenset({("snapshot",), ("snapshot", "-i")})
# Snapshot roles that hold typed text; the value follows the line's ref and attributes after ": ".
_FIELD_LINE = re.compile(r"^(?P<indent>\s*)- (?:textbox|searchbox|combobox|spinbutton)\b")
_REF = re.compile(r"\[ref=e\d+\]")
_ATTRIBUTES = re.compile(r"(?:\s*\[[a-z-]+(?:=[^\]\s]*)?\])*")
VALUE_HIDDEN = "[value hidden]"
# Locates an agent's ref or CSS selector on the page as a backendNodeId, or None when it can't.
Resolve = Callable[[str], Awaitable[int | None]]


@dataclass
class PasswordReadGuard:
    """Only node identities retained for a document; never a plan step or page content."""

    visit: str
    seen_nodes: frozenset[int] = frozenset()


# A document stays read-guarded independently of whether any step is open, and a thread keeps one guard
# per document it has guarded: leaving a tab never lifts it, and a loader id never recurs, so a new
# document is never over-refused. Cleared when the thread's browser closes.
_guarded_visits: dict[str, dict[str, PasswordReadGuard]] = {}


def pane_connected(thread_id: str, connection_token: str, report: Callable[[dict], None] | None = None,
                   probe: browser_pane_state.Probe | None = None, *,
                   session: Callable[[], str | None] | None = None) -> None:
    """Record a shared pane and notify the step owner when it can probe."""
    async def guarded_probe(_known: browser_pane_state.KnownNodes) -> PasswordProbe:
        assert probe is not None
        return await probe(_known_nodes(thread_id))

    browser_pane_state.pane_connected(
        thread_id, connection_token, report, guarded_probe if probe is not None else None,
        on_probe=lambda thread: browser_step_lifecycle.pane_event(thread, "pane_probe_available"), session=session,
        observer=partial(_observe_read_guard, thread_id),
    )


def pane_disconnected(thread_id: str, connection_token: str) -> None:
    """Notify the step owner once no pane remains to probe the page."""
    browser_pane_state.pane_disconnected(
        thread_id, connection_token,
        on_last_probe=lambda thread: browser_step_lifecycle.pane_event(thread, "pane_probe_gone"),
    )


@dataclass(frozen=True)
class PageAccess:
    """What a command the guard let through must carry: its note, and whether a snapshot hides field values."""

    note: str | None
    filled_field_visit: bool


async def require_page_access(thread_id: str, probe: browser_pane_state.Probe, args: tuple[str, ...], *,
                              resolve: Resolve | None = None, session: str | None = None) -> str | None:
    """Enforce page ownership, then the independent password-visit read guard.

    Returns STEP_NOT_RECORDED when no step is held and the page has no password field but its step couldn't
    be written; the command still runs.
    """
    return (await check_page_access(thread_id, probe, args, resolve=resolve, session=session)).note


async def check_page_access(thread_id: str, probe: browser_pane_state.Probe, args: tuple[str, ...], *,
                            resolve: Resolve | None = None, session: str | None = None) -> PageAccess:
    """require_page_access, also saying whether a snapshot's field values must be hidden (hide_field_values).

    A command passes with a password field visible only once the person marked it done (R14): every other
    path with one showing refuses first. From then until the visit ends, only _filled_field_allows commands run.
    """
    result, note = await _guarded_probe(thread_id, probe, session=session)
    if (guarded := _guarded_visits.get(thread_id, {}).get(result.visit)) is not None:
        await _refuse_password_read(guarded, args, resolve)
    filled = bool(result.visible_nodes) or browser_step_lifecycle.finished_visit(thread_id, result.visit)
    if filled and not _filled_field_allows(args):
        raise BrowserSessionError(FILLED_FIELD_REFUSED)
    return PageAccess(note, filled)


def _filled_field_allows(args: tuple[str, ...]) -> bool:
    if args[0] == "snapshot":
        return tuple(args) in _FILLED_FIELD_SNAPSHOTS
    if args[0] == "get":
        if args[1:2] == ("attr",):
            return len(args) == 4 and args[3].strip().casefold() != "value"
        return len(args) >= 2 and args[1] in _FILLED_FIELD_GETS
    if args[0] == "wait":
        position = 1
        while position < len(args):
            if args[position] in _FILLED_FIELD_WAIT_OPTIONS:
                # An operand that looks like an option (wait --text --fn ...) may parse as one.
                if position + 1 >= len(args) or args[position + 1].startswith("-"):
                    return False
                position += 2
            elif args[position].startswith("-"):
                return False
            else:
                position += 1
        return True
    return args[0] in _FILLED_FIELD_COMMANDS


def hide_field_values(snapshot: str) -> str:
    """Replace every text field's value in a snapshot, and drop the text nodes beneath it.

    Reads no value and needs no node identity: anything after a field's ref and attributes is hidden, so a
    line it can't parse fails closed.
    """
    lines, field_indent = [], None
    for line in snapshot.split("\n"):
        indent = len(line) - len(line.lstrip())
        if field_indent is not None and indent > field_indent and line.strip():
            continue
        field_indent = None
        if (match := _FIELD_LINE.match(line)) is None:
            lines.append(line)
            continue
        field_indent = len(match.group("indent"))
        ref = _REF.search(line, match.end())
        if ref is None:
            lines.append(f"{line[:match.end()]} {VALUE_HIDDEN}")
            continue
        attributes = _ATTRIBUTES.match(line, ref.end())
        kept = attributes.end() if attributes is not None else ref.end()
        lines.append(line if not line[kept:].strip() else f"{line[:kept]}: {VALUE_HIDDEN}")
    return "\n".join(lines)


async def declare_person_step(thread_id: str, probe: browser_pane_state.Probe, instruction: str, *,
                              session: str | None = None) -> None:
    """Give the page to the person; an open declaration is a no-op, a decline is final for its visit."""
    instruction = str(instruction).strip()
    if not instruction:
        raise BrowserSessionError("browser person step needs an instruction")
    _, note = await _guarded_probe(thread_id, probe, session=session, instruction=instruction)
    if note is not None:
        raise BrowserSessionError(DECLARATION_NOT_RECORDED)


async def _guarded_probe(thread_id: str, probe: browser_pane_state.Probe, *, session: str | None = None,
                         instruction: str | None = None) -> tuple[PasswordProbe, str | None]:
    """Share the agent probe and failure mapping between commands and declarations.

    A step that can't be written is logged and a live step is kept (the next call or re-probe recovers it);
    the page's password guard still applies. A held step refuses every command, close included, as an
    open step does (R7): its task exists, so its normal refusal is true. With no step held, a page showing a
    password field refuses the command, and any other page runs it and the agent gets STEP_NOT_RECORDED.
    """
    try:
        result = await probe(_known_nodes(thread_id))
    except Exception as exc:
        if isinstance(exc, BrowserSessionError) and str(exc) == PAGE_CHECK_FAILED:
            raise
        log.warning("browser page check failed for thread %s", thread_id, exc_info=True)
        raise BrowserSessionError(PAGE_CHECK_FAILED) from exc
    event = browser_step_lifecycle.Event(
        "declare" if instruction is not None else "probe", result,
        session=session, instruction=instruction or "",
    )
    try:
        decision = await browser_step_lifecycle.step_event(thread_id, event, observer=partial(_observe_read_guard, thread_id))
    except Exception:
        log.warning("browser step for thread %s could not be recorded; a live step is kept for the next call",
                    thread_id, exc_info=True)
        _observe_read_guard(thread_id, result, False, False)
        # A held step means its task exists (a failed row read, settle or emit leaves it), so the page is the
        # person's and its normal refusal is true; the kind comes from in-memory state, not the store that failed.
        if browser_step_lifecycle.has_step(thread_id):
            if browser_step_lifecycle.password_page(thread_id):
                raise BrowserSessionError(PASSWORD_PAGE + _ending(thread_id))
            raise BrowserSessionError(PERSON_STEP)
        # With no step held, the lifecycle's own visible-password test (R9) still marks the page as the person's.
        if result.visible_nodes:
            raise BrowserSessionError(PASSWORD_PAGE_UNRECORDED)
        return result, STEP_NOT_RECORDED
    messages = {"password": PASSWORD_PAGE + _ending(thread_id), "person": PERSON_STEP,
                "password_declined": PASSWORD_DECLINED, "person_declined": PERSON_STEP_DECLINED}
    if decision.refusal is not None:
        raise BrowserSessionError(messages[decision.refusal])
    return result, None


def _observe_read_guard(thread_id: str, result: PasswordProbe | None, clear_session: bool, guard_present: bool) -> None:
    """Apply the decided observation before plan effects, keeping all read-guard state local."""
    if clear_session:
        browser_closed(thread_id)
    if result is not None and (result.visible_nodes or guard_present and result.present_nodes):
        _guard(thread_id, result, nodes=result.present_nodes | result.visible_nodes)


def _ending(thread_id: str) -> str:
    if browser_pane_state.has_pane(thread_id):
        return " Post nothing in chat."
    return " If you reply, use one short line; don't restate the task or say you made it."


def _guard(thread_id: str, result: PasswordProbe, *, nodes: frozenset[int] | None = None) -> None:
    """Retain password node identities independently of the step lifecycle (AR2)."""
    guards = _guarded_visits.setdefault(thread_id, {})
    guarded = guards.setdefault(result.visit, PasswordReadGuard(result.visit))
    guarded.seen_nodes = guarded.seen_nodes | (result.visible_nodes if nodes is None else nodes)


def _known_nodes(thread_id: str) -> dict[str, frozenset[int]]:
    """Each guarded visit's password node identities, for the probe to match against the live page's visit."""
    return {visit: guarded.seen_nodes for visit, guarded in _guarded_visits.get(thread_id, {}).items()}


def person_input(thread_id: str) -> None:
    """Note input for the lifecycle owner's re-probe cadence."""
    browser_step_lifecycle.person_input(thread_id)


def browser_closed(thread_id: str) -> None:
    """Clear read guards and agent presence; the session hook owns step cleanup."""
    browser_pane_state.agent_left(thread_id)
    _guarded_visits.pop(thread_id, None)


async def _refuse_password_read(guarded: PasswordReadGuard, args: tuple[str, ...], resolve: Resolve | None) -> None:
    """Keep a password visit's DOM reads unavailable after its field is hidden (AR2).

    Reached only with no step open (an open step refuses every command first), so the refusal names
    the guard alone, never the person's step.
    """
    if args and args[0] == "eval":
        raise BrowserSessionError(PASSWORD_READ_REFUSED)
    if len(args) >= 3 and args[0] == "get" and args[1] in {"value", "attr"}:
        try:
            node = await resolve(args[2]) if resolve is not None else None
        except Exception as exc:
            log.info("password-visit read target for %s could not be located: %s", guarded.visit, exc)
            node = None
        # A target that can't be located may be the hidden password input itself, so it fails closed.
        if node is None or node in guarded.seen_nodes:
            raise BrowserSessionError(PASSWORD_READ_REFUSED)


def password_page(thread_id: str) -> bool:
    """Expose only this thread's password-page boolean to internal consumers."""
    return browser_step_lifecycle.password_page(thread_id)


def announce_browser_opened(thread_id: str) -> None:
    """Tell the thread's pages a successful browser open is now streamable."""
    state.broadcast_to_thread(thread_id, {"type": "browser_opened", "thread_id": thread_id})
