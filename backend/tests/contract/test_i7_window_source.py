"""I7 — Real window only: one freshly-validated window, never a raw overflow.

Contract clause: *"Fit math, pressure triggers, and the widget read one
freshly-validated provider window; a provider overflow rejection is treated as
a discovery failure (re-discover, re-fit, retry) — never surfaced raw."*

The incident (2026-08-07, issue #1024): oMLX's serving window for a local model
dropped from 1M to ~503,808 while CORSIA's catalog kept 1,048,576. Fit math
passed against a window that did not exist; the provider rejected, and its
sentence — "Message too long for …'s context window. Try a shorter conversation
or clear history." — became the turn's terminal error on the user's screen;
pressure-checkpointing never fired, being aimed at the fiction. That morning one
thread's chat turns used 1,048,576 while its fold used 503,808.

Reds are the pre-collapse state; each message names what to demolish.
"""

from __future__ import annotations

import json
import re

import pytest

from tests.contract.conftest import BACKEND_ROOT, iter_backend_sources

pytestmark = pytest.mark.contract

# The incident model: a discovered oMLX row still stored at the stale 1M.
MODEL_ID = "i7-omlx-window"
STALE_WINDOW = 1_048_576
REAL_WINDOW = 503_808
# Nothing is saved for this row, so both incident windows now count against the
# CORSIA checkpoint default; a below-default move keeps the freshness pin visible.
MOVED_WINDOW = 200_000
DEFAULT_WINDOW = 262_144

# The one module allowed to read a model's stored window; everything else
# derives from it. Discovery writers populate it, so they are the source.
WINDOW_RESOLVER_MODULE = "llm/context_window.py"
CATALOG_DISCOVERY_WRITERS = {
    "connections/harness_model_rows.py",
    "connections/harness_models.py",
    "llm/catalog_cloud.py",
    "llm/catalog_network_models.py",
    "llm/external_provider.py",
    "llm/harness_catalog.py",
}

_STORED_WINDOW_READ = re.compile(
    r"(?:\bcontext_window\b[^;\"']{0,120}?\bFROM\s+(?:models|model_profiles)\b)"
    r"|(?:\.get\(\s*[\"']context_window[\"']\s*\))"
    r"|(?:\[\s*[\"']context_window[\"']\s*\])",
    re.IGNORECASE | re.DOTALL,
)


def _discover_provider_window(window: int, *, override: int | None = None) -> None:
    """Write what discovery writes: the catalog row, plus any operator override."""
    from infra.database import get_db

    db = get_db()
    try:
        db.execute(
            """INSERT OR REPLACE INTO models
               (id,harness,provider,provider_model_id,litellm_model,display_name,
                backend,source,context_window,created_at,updated_at)
               VALUES (?,'native','omlx','i7-window-model',
                       'omlx@i7srv/i7-window-model','I7 Window Model','network',
                       'test',?,'t','t')""",
            (MODEL_ID, window),
        )
        db.execute("DELETE FROM model_profiles WHERE model_id = ?", (MODEL_ID,))
        if override is not None:
            db.execute(
                "INSERT INTO model_profiles (model_id, context_window) VALUES (?,?)",
                (MODEL_ID, override),
            )
        db.commit()
    finally:
        db.close()


# --- (a) SINGLE SOURCE ----------------------------------------------------
def test_every_window_consumer_reports_one_number(isolated_db):
    """One model, one window — on every surface that reports one.

    The runtime form of "two subsystems, two numbers, same model": dispatch
    resolution, the capabilities helper, the switcher rows, the settings pane,
    and the fit check must not be able to disagree about one model.
    """
    from api.provider_status import _catalog_row_to_model_view
    from core.context_admission import check_context_preflight
    from infra.database import get_db
    from llm.capabilities import get_model_capabilities, get_model_context_window
    from llm.catalog import get_model
    from llm.model_status import get_model_status_rows
    from llm.resolution import resolve_execution_target
    from shared_send_fixtures import _insert_session

    _discover_provider_window(STALE_WINDOW, override=REAL_WINDOW)
    _insert_session("i7-single-source", model=MODEL_ID, spawned_by=None)
    rows = get_model_status_rows()
    status_row = next((r for r in rows if r["id"] == MODEL_ID), {})
    db = get_db()
    try:
        preflight = check_context_preflight(
            thread_id="i7-single-source", model=MODEL_ID,
            current_user_content="hello", db=db,
        )
    finally:
        db.close()
    reported = {
        "llm/context_window.py::get_context_window":
            get_model_context_window(MODEL_ID),
        "llm/resolution.py::resolve_execution_target.context_window":
            resolve_execution_target(MODEL_ID).context_window,
        "llm/capabilities.py::get_model_capabilities.context_window_max":
            get_model_capabilities(MODEL_ID).get("context_window_max"),
        "llm/model_status.py::get_model_status_rows.context_window_max":
            (status_row.get("capabilities") or {}).get("context_window_max"),
        "api/provider_status.py::_catalog_row_to_model_view.context_length":
            _catalog_row_to_model_view(get_model(MODEL_ID) or {}, {}).get("context_length"),
        "core/context_admission.py::check_context_preflight.window":
            preflight.model_context_window,
    }
    distinct = sorted({value for value in reported.values()})
    assert len(distinct) == 1, (
        f"model {MODEL_ID!r} has {len(distinct)} different context windows "
        f"{distinct} across consumer-facing surfaces. Fit math, pressure "
        "triggers, and the widget must read one resolution. Each surface and "
        "the number it reports:\n"
        + "\n".join(f"  {name} -> {value}" for name, value in reported.items())
    )


def test_one_module_reads_the_stored_window():
    """Only the window resolver may read the stored window; others derive.

    The static worklist behind the divergence above: a second module reading
    ``models.context_window``/``model_profiles.context_window`` re-implements
    the rule, and the copies drift.
    """
    sanctioned = {WINDOW_RESOLVER_MODULE} | CATALOG_DISCOVERY_WRITERS
    found: list[tuple[str, int, str]] = []
    for path, text in iter_backend_sources():
        rel = str(path.relative_to(BACKEND_ROOT))
        if rel in sanctioned:
            continue
        lines = text.splitlines()
        for match in _STORED_WINDOW_READ.finditer(text):
            lineno = text.count("\n", 0, match.start()) + 1
            source = lines[lineno - 1].strip() if lineno <= len(lines) else ""
            found.append((rel, lineno, source))

    violations = [f"{rel}:{no}  {src}" for rel, no, src in sorted(found)]
    assert not violations, (
        f"{len(violations)} independent read(s) of a model's stored context "
        f"window outside {WINDOW_RESOLVER_MODULE}. Each must derive its number "
        "from the single resolution path (discovery writers "
        f"{sorted(CATALOG_DISCOVERY_WRITERS)} are the source and are exempt); "
        "this list is the worklist:\n" + "\n".join(violations)
    )


# --- (b) FRESHNESS --------------------------------------------------------
def test_fit_math_uses_the_window_the_catalog_now_holds(isolated_db):
    """A window change at the provider reaches the very next fit computation.

    The pin against a process-lifetime window cache.
    """
    from core.context_admission import check_context_preflight
    from infra.database import get_db
    from shared_send_fixtures import _insert_session

    _discover_provider_window(STALE_WINDOW)
    _insert_session("i7-freshness", model=MODEL_ID, spawned_by=None)

    def _window() -> int:
        db = get_db()
        try:
            return check_context_preflight(
                thread_id="i7-freshness", model=MODEL_ID,
                current_user_content="hello", db=db,
            ).model_context_window
        finally:
            db.close()

    before = _window()
    assert before == DEFAULT_WINDOW, (
        f"fit math reported {before} for a row with nothing saved; the "
        f"provider's {STALE_WINDOW} counts against the CORSIA default."
    )
    _discover_provider_window(REAL_WINDOW)
    after = _window()
    assert after == DEFAULT_WINDOW, (
        "fit math served a stale window after the provider window moved "
        f"({before} -> {REAL_WINDOW}); it still computed against {after}. A "
        "window is a runtime fact, never a cached one."
    )
    # The default must not become the cache it replaces: the first move below it
    # reaches the very next computation.
    _discover_provider_window(MOVED_WINDOW)
    assert _window() == MOVED_WINDOW, (
        f"fit math kept {DEFAULT_WINDOW} after the catalog moved below it to "
        f"{MOVED_WINDOW}; the default froze the window it is meant to cap."
    )


def _thread_with_measured_occupancy(
    thread_id: str, *, measured_tokens: int, recorded_window: int,
) -> str:
    """Seed one settled native thread whose CTX proof recorded ``window``."""
    from infra.context_receipts import ContextReceiptIdentity, attach_context_receipt
    from infra.database import get_db
    from shared_send_fixtures import _insert_session

    _insert_session(thread_id, model=MODEL_ID, spawned_by=None)
    turn_id = f"{thread_id}-turn"
    message_id = f"{thread_id}-a1"
    db = get_db()
    try:
        db.execute(
            "INSERT INTO active_turns "
            "(id,thread_id,execution_epoch,model,status,started_at,ended_at,boot_epoch) "
            "VALUES (?,?,0,?,'completed','start','end','boot')",
            (turn_id, thread_id, MODEL_ID),
        )
        db.execute(
            "INSERT INTO messages "
            "(id,thread_id,role,content,metadata,created_at,seq,delivery_state) "
            "VALUES (?,?,'assistant','done',?,'2026-08-07T00:00:01Z',1,'delivered')",
            (message_id, thread_id, json.dumps({"turn_id": turn_id})),
        )
        attach_context_receipt(
            thread_id,
            message_id,
            context_usage={
                "snapshot_id": f"{thread_id}-snap",
                "token_usage_available": True,
                "token_source": "measured",
                "total_used": measured_tokens,
                "model_context_window": recorded_window,
            },
            identity=ContextReceiptIdentity("native", MODEL_ID, 0),
            db=db,
        )
        db.commit()
    finally:
        db.close()
    return message_id


def test_pressure_trigger_uses_the_current_window_not_the_recorded_one(isolated_db):
    """Pressure math must aim at the window the provider serves now.

    The exact silence in #1024: the last turn's CTX proof recorded 1,048,576,
    the provider dropped to 503,808, and the capacity anchor kept feeding the
    recorded number to the predicate. 600k measured tokens fit the fiction and
    overflow the fact.
    """
    from core.fork_fit import (
        BoundaryDecision,
        ProviderPayload,
        evaluate_boundary,
    )
    from llm.capabilities import get_model_context_window

    _discover_provider_window(STALE_WINDOW)
    _thread_with_measured_occupancy(
        "i7-pressure", measured_tokens=600_000, recorded_window=STALE_WINDOW,
    )
    _discover_provider_window(REAL_WINDOW)
    current_window = get_model_context_window(MODEL_ID)

    def decision(window: int) -> BoundaryDecision:
        return evaluate_boundary(ProviderPayload(
            context_window=window,
            canonical_input_tokens=600_000,
            input_tokens=600_000,
            minimum_tokens=1_000,
            checkpoint_status=None,
            installed_input_tokens=None,
            checkpoint_admissible=True,
        ))

    # Nothing is saved, so the current window is the CORSIA default — still not
    # the 1,048,576 the last turn's receipt recorded.
    assert current_window == DEFAULT_WINDOW
    assert decision(current_window) is BoundaryDecision.WAIT
    assert decision(STALE_WINDOW) is BoundaryDecision.PROCEED


def test_widget_preview_uses_the_current_window_not_the_recorded_one(isolated_db):
    """The next-turn projection uses the live window, not a prior receipt's."""
    from core.context_preview import build_next_turn_context_preview
    from infra.context_receipts import (
        ContextReceiptIdentity,
        latest_measured_context_receipt,
    )
    from infra.database import get_db
    from infra.queries import get_thread_for_user

    _discover_provider_window(STALE_WINDOW)
    _thread_with_measured_occupancy(
        "i7-widget", measured_tokens=600_000, recorded_window=STALE_WINDOW,
    )
    _discover_provider_window(REAL_WINDOW)

    db = get_db()
    try:
        thread_row = get_thread_for_user("i7-widget", "u-test", db=db)
        preview = build_next_turn_context_preview(
            "i7-widget", thread_row, db=db, current_user_content="",
        )
        measured = latest_measured_context_receipt(
            "i7-widget", ContextReceiptIdentity("native", MODEL_ID, 0), db=db,
        )
    finally:
        db.close()
    assert measured is not None, "the seeded thread must expose a measured proof"
    assert measured["context_usage"]["model_context_window"] == STALE_WINDOW
    # Nothing is saved, so the live projection counts against the CORSIA
    # default; either way it is not the window the recorded receipt names.
    assert preview["model_context_window"] == DEFAULT_WINDOW, (
        f"the next-turn preview kept {preview['model_context_window']} after "
        f"the provider window moved to {REAL_WINDOW}; a terminal receipt is "
        "historical and cannot own the widget projection"
    )


# --- (c) OVERFLOW IS A DISCOVERY FAILURE, NOT A USER-FACING STRING --------
# The provider sentence from the incident, verbatim in shape, and the private
# overflow detector inside ``llm/base.py::_humanize_error`` that answers it.
PROVIDER_OVERFLOW_REJECTION = (
    "litellm.BadRequestError: OpenAIException - This model's maximum context "
    "length is 503808 tokens, however you requested 700000 tokens"
)
_PRIVATE_OVERFLOW_DETECTOR = re.compile(
    r"[\"'](?:context length|maximum context)[\"']\s+in\s+msg"
)


def _humanizer_overflow_site() -> str:
    """Locate the branch that renders an overflow as a user-facing sentence."""
    text = (BACKEND_ROOT / "llm" / "base.py").read_text()
    index = text.find("Message too long for")
    if index < 0:
        return "llm/base.py (overflow humanizer branch removed)"
    return f"llm/base.py:{text.count(chr(10), 0, index) + 1}"


@pytest.mark.asyncio
async def test_provider_overflow_is_never_surfaced_raw(monkeypatch):
    """A context-overflow rejection is a discovery failure, not a message.

    A rejection for size proves our window was wrong; the contract response is
    re-discover, re-fit, retry. Production instead rewrites it into a sentence,
    returns it as assistant content with ``finish_reason='error'``, and raises
    it as the turn's ``TurnProviderError`` — telling the user to "clear history"
    for a window CORSIA measured wrong.
    """
    from llm import base as llm_base

    async def _reject(**_kwargs):
        raise Exception(PROVIDER_OVERFLOW_REJECTION)

    monkeypatch.setattr(llm_base, "acompletion", _reject)
    response = await llm_base.base_completion(
        [{"role": "user", "content": "hi"}],
        model=MODEL_ID,
        retry_transient_errors=False,
    )

    surfaced = response.content if isinstance(response.content, str) else ""
    assert not (response.finish_reason == "error" and surfaced), (
        "a provider context-overflow rejection was returned as user-facing "
        f"turn content: {surfaced!r}\n"
        f"  rendered at {_humanizer_overflow_site()} (llm/base.py::_humanize_error)\n"
        "  returned at llm/base.py::base_completion as finish_reason='error'\n"
        "  raised at core/turn_iteration.py as TurnProviderError\n"
        "This rejection must re-enter discovery — re-read the window, re-fit, "
        "retry; the user sees a handled transition, never the provider's line."
    )


def test_one_classifier_recognizes_the_incident_rejection():
    """Overflow classification lives in one place and knows the real wording.

    Two detectors exist: ``subagents/provider_context_limits.py`` for the
    harnesses, an inline substring test in ``llm/base.py`` for native, and they
    disagree both ways. You cannot re-discover what you cannot classify, so
    this is the precondition for the retry loop above.
    """
    from subagents.provider_context_limits import is_provider_context_limit

    assert is_provider_context_limit(PROVIDER_OVERFLOW_REJECTION), (
        "the shared provider-overflow classifier does not recognize the "
        f"rejection that caused #1024: {PROVIDER_OVERFLOW_REJECTION!r}. Its "
        "markers in subagents/provider_context_limits.py cover 'context window "
        "exceeded' / 'prompt is too long' but not \"maximum context length is N "
        "tokens\" — the wording oMLX and OpenAI actually emit."
    )
    private = _PRIVATE_OVERFLOW_DETECTOR.search(
        (BACKEND_ROOT / "llm" / "base.py").read_text()
    )
    assert private is None, (
        f"{_humanizer_overflow_site()} carries a second, private "
        f"context-overflow detector ({private.group(0)!r} in "
        "llm/base.py::_humanize_error). Native dispatch must classify overflow "
        "through subagents/provider_context_limits.py::is_provider_context_limit "
        "like every other harness; today the two already disagree both ways."
    )
