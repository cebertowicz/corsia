"""Opt-in recorder for raw provider frames, for building replay fixtures.

The Claude Code and codex event pumps are the least-covered part of the turn
runtime: nothing replays a real provider stream against them, so a refactor
cannot be shown to preserve what the provider actually sends. Fixtures for that
replay have to be *recorded*, because a hand-authored stream encodes what we
believe the provider emits, and a decomposition certified against our beliefs
is certified against nothing.

This records the frames verbatim at the exact seam each pump consumes:

* Claude Code — one parsed line of ``claude -p --output-format stream-json``
  stdout, before ``normalize_event`` (``cc_stream._read_loop``).
* codex — one parsed inbound JSON-RPC message (``_handle_message``).

Disabled unless explicitly configured, and then only for named threads:

* ``CORSIA_PROVIDER_STREAM_CAPTURE_DIR`` — directory to append into.
* ``CORSIA_PROVIDER_STREAM_CAPTURE_THREADS`` — comma-separated CORSIA thread
  ids to record.

**Both are required and the allowlist is never a wildcard.** These fixtures are
destined for a public repository, and a provider stream carries the user's
actual conversation — their messages, file contents, and repository. An
allowlist that could default to "everything" would make publishing private
working data a configuration slip. Requiring the exact thread ids means a
capture can only ever contain a thread whose id was deliberately typed, so the
scratch thread is the only thing that can be recorded.

Recording never affects delivery: it appends, it parses nothing, and any I/O
failure is logged and dropped rather than raised into the read loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import TextIO

log = logging.getLogger(__name__)

_DIR_VAR = "CORSIA_PROVIDER_STREAM_CAPTURE_DIR"
_THREADS_VAR = "CORSIA_PROVIDER_STREAM_CAPTURE_THREADS"

#: An allowlist entry must be a literal thread id. The character class is what
#: makes that true: every way of writing a pattern — ``*``, ``%``, ``.*``,
#: ``prefix-*`` — contains a character outside it and is dropped, so no entry
#: can ever match more than the one thread whose id someone typed. Entries are
#: then compared by equality, never by match, which is the actual protection;
#: the class only stops a pattern from looking like it was accepted.
_THREAD_ID = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _allowlist() -> frozenset[str]:
    """Exact CORSIA thread ids configured for capture; empty disables it."""
    raw = os.environ.get(_THREADS_VAR) or ""
    return frozenset(
        candidate
        for candidate in (part.strip() for part in raw.split(","))
        if candidate and _THREAD_ID.match(candidate)
    )


def capture_dir_for(corsia_thread_id: str | None) -> Path | None:
    """Return the capture directory when this exact thread is allowlisted.

    ``None`` — the overwhelmingly common case — means every call site below is
    a branch and a return.
    """
    if not corsia_thread_id:
        return None
    directory = os.environ.get(_DIR_VAR)
    if not directory:
        return None
    if corsia_thread_id not in _allowlist():
        return None
    return Path(directory)


#: One open append handle per capture file. Reopening per frame cost an extra
#: three syscalls each (stat for mkdir, open, close) on the event-loop thread.
_HANDLES: dict[Path, "TextIO"] = {}

#: Hard ceiling on retained descriptors. The allowlist is human-typed and a
#: capture session targets one scratch thread, so two harnesses plus headroom
#: is generous. The cap exists because "bounded in practice" is not bounded:
#: nothing stops a long-lived process from accumulating a handle per recorded
#: thread until it exhausts its descriptors, and a recorder must not be able
#: to degrade the process it observes.
_MAX_HANDLES = 8


def _handle_for(path: Path) -> "TextIO":
    """Open this capture file once and keep appending to it.

    Retains at most ``_MAX_HANDLES``; the least recently opened is closed
    first. Eviction only closes a file — the next frame for that path simply
    reopens it in append mode, so no capture is lost or truncated.
    """
    handle = _HANDLES.get(path)
    if handle is not None and not handle.closed:
        return handle
    while len(_HANDLES) >= _MAX_HANDLES:
        oldest_path = next(iter(_HANDLES))
        evicted = _HANDLES.pop(oldest_path)
        try:
            evicted.close()
        except OSError:  # a file we can no longer flush is not worth raising for
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    _HANDLES[path] = handle
    return handle


def close_captures() -> None:
    """Close every open capture file; safe to call when none are open."""
    for path, handle in list(_HANDLES.items()):
        try:
            handle.close()
        except OSError:  # a file we can no longer flush is not worth raising for
            pass
        _HANDLES.pop(path, None)


def record_frame(
    corsia_thread_id: str | None, harness: str, frame: dict,
) -> None:
    """Append one raw provider frame to this thread's capture file.

    ``harness`` is ``cc`` or ``codex``; frames land in
    ``<dir>/<harness>-<thread id>.jsonl`` as one compact JSON object per line,
    which is the shape the replay loader reads back.

    **Deliberately synchronous.** This runs on the event-loop thread, and
    offloading it to a thread or task would let two frames land out of order.
    A fixture whose frames are reordered is worse than no fixture: it would
    certify a pump against a sequence the provider never sent. Ordering is the
    fixture's integrity, so the write stays in-line and is made cheap instead
    — one buffered ``write`` plus ``flush`` against a handle opened once,
    rather than the mkdir/open/write/close per frame this used to do.

    The flush is kept on purpose: a capture exists to be replayed after the
    process it recorded is gone, including when that process was killed.
    """
    directory = capture_dir_for(corsia_thread_id)
    if directory is None:
        return
    try:
        handle = _handle_for(directory / f"{harness}-{corsia_thread_id}.jsonl")
        handle.write(json.dumps(frame, separators=(",", ":")) + "\n")
        handle.flush()
    except (OSError, TypeError, ValueError) as exc:
        # A diagnostic recorder must never be able to end a turn.
        log.warning(
            "stream_capture: dropped a %s frame for %s: %s",
            harness, corsia_thread_id, exc,
        )
