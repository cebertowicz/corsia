"""Recorded provider-stream fixtures: format, loader, and realism validator.

A fixture here is the raw bytes a provider actually sent, recorded by
``subagents.stream_capture`` at the exact seam the pump consumes, one compact
JSON object per line:

* ``cc-<scenario>.jsonl`` — parsed ``claude -p --output-format stream-json``
  stdout lines, the input to ``cc_events.normalize_event``.
* ``codex-<scenario>.jsonl`` — inbound app-server JSON-RPC messages.

Each carries a sidecar ``<name>.meta.json`` recording how it was captured.

**Why provenance is enforced rather than documented.** The entire value of a
recorded fixture is that it encodes what the provider *does*, not what we
believe it does; a hand-authored file dropped in later looks identical and
silently converts the replay suite back into a test of our own assumptions.
``validate_fixture`` therefore fails a fixture whose provenance is missing or
whose method is not a recorded capture. The format cannot express "trust me".

**Why realism is checked against the pump's own grammar.** A fixture that
parses but normalizes to a kind no pump dispatches exercises nothing while
looking like coverage — the failure mode that made four separate suites report
green over nothing. Validation runs the real normalizer and requires that the
frames reach dispatchable branches.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: Kinds ``cc_runtime_events._make_on_event`` dispatches. Kept here so a
#: fixture can be judged without constructing a pump; a test asserts this
#: equals the live dispatch table, so a pump that gains a branch cannot leave
#: the validator silently behind.
CC_DISPATCH_KINDS = frozenset({
    "thinking_delta", "reasoning_provenance", "final_call_usage", "text_delta",
    "assistant_step", "stream_line_dropped", "tool_result", "result",
    "tool_boundary_violation", "init", "rate_limit",
    "tool_input_start", "tool_input_delta", "tool_input_stop",
})

#: Kinds ``codex_runtime_events.make_on_event`` dispatches.
CODEX_DISPATCH_KINDS = frozenset({
    "thread_started", "text_delta", "assistant_text", "step_reasoning",
    "stream_line_dropped", "tool_use", "tool_result", "result", "turn_failed",
    "provider_tool_violation", "model_reported", "call_usage",
})

HARNESSES = ("cc", "codex")

#: A capture must name how it was produced. Only these mean "recorded".
RECORDED_METHODS = frozenset({"stream_capture_tap"})

#: Content that must never reach a public repository. Absolute home paths and
#: repository paths identify the machine and the user's working data.
_LEAK_PATTERNS = (
    (re.compile(r"/Users/[^/\"\s]+"), "an absolute macOS home path"),
    (re.compile(r"/home/[^/\"\s]+"), "an absolute Linux home path"),
    (re.compile(r"/Volumes/[^/\"\s]+"), "an absolute mounted-volume path"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "an API key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "a GitHub token"),
)


class FixtureError(AssertionError):
    """A fixture is unusable as evidence; the message says exactly why."""


@dataclass(frozen=True)
class StreamFixture:
    """One recorded provider stream and its capture provenance."""

    name: str
    harness: str
    path: Path
    frames: list[dict]
    meta: dict

    @property
    def scenario(self) -> str:
        """The scenario this capture covers (tool_heavy, refusal, …)."""
        return str(self.meta.get("scenario") or "")


def fixture_root() -> Path:
    """Directory holding recorded provider-stream fixtures."""
    return Path(__file__).parent / "fixtures" / "provider_streams"


def load_fixture(path: str | Path) -> StreamFixture:
    """Read one ``.jsonl`` capture and its ``.meta.json`` sidecar.

    Raises ``FixtureError`` rather than returning a partial fixture: a stream
    that silently loses its unparseable frames would let a truncated capture
    pass as a complete one.
    """
    path = Path(path)
    if not path.exists():
        raise FixtureError(f"no fixture at {path}")
    harness = path.name.split("-", 1)[0]
    if harness not in HARNESSES:
        raise FixtureError(
            f"{path.name}: name must start with one of {sorted(HARNESSES)}"
        )

    frames: list[dict] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path.name} line {number}: not JSON ({exc})")
        if not isinstance(frame, dict):
            raise FixtureError(
                f"{path.name} line {number}: expected an object, got "
                f"{type(frame).__name__}"
            )
        frames.append(frame)
    if not frames:
        raise FixtureError(f"{path.name}: no frames; an empty capture proves nothing")

    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        raise FixtureError(
            f"{path.name}: no {meta_path.name}. A capture without provenance is "
            "indistinguishable from a hand-authored file"
        )
    try:
        meta = json.loads(meta_path.read_text())
    except json.JSONDecodeError as exc:
        raise FixtureError(f"{meta_path.name}: not JSON ({exc})")

    return StreamFixture(
        name=path.stem, harness=harness, path=path, frames=frames, meta=meta,
    )


def load_all(harness: str | None = None) -> list[StreamFixture]:
    """Every recorded fixture, optionally for one harness."""
    root = fixture_root()
    if not root.exists():
        return []
    pattern = f"{harness}-*.jsonl" if harness else "*.jsonl"
    return [load_fixture(path) for path in sorted(root.glob(pattern))]


def normalize(fixture: StreamFixture) -> list[dict]:
    """Run the fixture's raw frames through the real production normalizer.

    This is the point of a recorded fixture: the same function the live reader
    calls, on bytes the provider actually sent.
    """
    if fixture.harness == "cc":
        from subagents.cc_events import normalize_event

        events: list[dict] = []
        for frame in fixture.frames:
            if frame.get("type") == "control_response":
                continue  # resolved by the transport, never normalized
            events.extend(normalize_event(frame))
        return events
    raise FixtureError(
        f"{fixture.name}: no normalizer wired for harness {fixture.harness!r}"
    )


def dispatch_kinds(harness: str) -> frozenset[str]:
    """Event kinds the named harness's pump actually dispatches."""
    if harness == "cc":
        return CC_DISPATCH_KINDS
    if harness == "codex":
        return CODEX_DISPATCH_KINDS
    raise FixtureError(f"unknown harness {harness!r}")


def _provenance_problems(fixture: StreamFixture) -> list[str]:
    """Provenance that would let a hand-authored file pass as a capture."""
    problems: list[str] = []
    method = fixture.meta.get("capture_method")
    if method not in RECORDED_METHODS:
        problems.append(
            f"capture_method={method!r} is not a recorded capture "
            f"(expected one of {sorted(RECORDED_METHODS)})"
        )
    for field in ("captured_at", "provider_version", "scenario", "scrubbed"):
        if not fixture.meta.get(field):
            problems.append(f"missing provenance field {field!r}")
    if fixture.meta.get("scrubbed") is not True:
        problems.append(
            "scrubbed is not true; a fixture must be scrubbed before it is usable"
        )
    return problems


def scrub_problems(fixture: StreamFixture) -> list[str]:
    """Host paths, credentials, or foreign thread ids left in a capture.

    These fixtures are destined for a public repository, where a mistake is
    permanent, so this runs on the raw text rather than on parsed fields.
    """
    problems: list[str] = []
    text = fixture.path.read_text()
    for pattern, description in _LEAK_PATTERNS:
        found = sorted(set(pattern.findall(text)))
        if found:
            problems.append(
                f"contains {description}: {', '.join(found[:3])}"
                + (" …" if len(found) > 3 else "")
            )
    allowed = fixture.meta.get("thread_id")
    if allowed:
        for other in sorted(set(re.findall(r"\bthread-[A-Za-z0-9_-]+", text))):
            if other != allowed:
                problems.append(f"contains a foreign thread id: {other}")
    return problems


def validate_fixture(fixture: StreamFixture) -> list[str]:
    """Every reason this fixture cannot serve as evidence. Empty means usable.

    Checks provenance, scrubbing, and realism — that the raw frames normalize
    through production code into kinds the pump actually dispatches. A fixture
    reaching no dispatchable branch is reported, because it would otherwise
    contribute passing tests that exercise nothing.
    """
    problems = _provenance_problems(fixture)
    problems.extend(scrub_problems(fixture))

    try:
        events = normalize(fixture)
    except FixtureError:
        raise
    except Exception as exc:  # noqa: BLE001 — a normalizer raise is the finding
        problems.append(
            f"production normalizer raised {type(exc).__name__}: {exc}"
        )
        return problems

    accepted = dispatch_kinds(fixture.harness)
    seen = {event.get("kind") for event in events}
    reached = seen & accepted
    if not reached:
        problems.append(
            f"normalizes to {sorted(k for k in seen if k)} — none of which the "
            f"{fixture.harness} pump dispatches, so this fixture exercises nothing"
        )
    unknown = {kind for kind in seen if kind and kind not in accepted}
    if unknown:
        problems.append(
            f"normalizes to kinds the {fixture.harness} pump does not dispatch: "
            f"{sorted(unknown)} — either the fixture is wrong or the pump changed"
        )
    return problems
