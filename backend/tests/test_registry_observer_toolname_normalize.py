"""Cross-harness parity: registration must fire identically regardless of
which tool-name spelling a harness persisted the call under.

Reproduces the real regression — the harness-tools unification renamed
tools to the ``mcp__corsia__*`` surface while the observer's match tables
still keyed on old exact names, so auto-registration silently stopped
firing. See backend/registry/observer.py's normalization seam
(``tools.catalog.normalize_tool_name``).
"""

from __future__ import annotations

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from core.step_handler import StepHandler  # noqa: E402
from core.turn_core import TurnIdentity # noqa: E402
from infra.config import WORKSPACE as _WS  # noqa: E402
from infra.database import get_db  # noqa: E402
from registry.observer import _NATIVE_TOOLS  # noqa: E402
from registry.observer import observe_record  # noqa: E402
from tests.registry_helpers import (  # noqa: E402
    artifact_rows_for_thread,
    cc_child,
    make_session,
)
from tools.catalog import TOOL_CATALOG  # noqa: E402


def _mk_session(db) -> str:
    return make_session(db, prefix="obs-norm")


def _artifacts(db, sid: str) -> list[dict]:
    return artifact_rows_for_thread(db, sid)


async def _noop_broadcast(_msg: dict) -> None:
    pass


def _handler(sid: str) -> StepHandler:
    handler = StepHandler(
        sid, {"model": "m", "model_name": "m", "is_cloud": False},
        _noop_broadcast, turn_id="turn-test",
    )
    handler.bind_turn_identity(TurnIdentity("turn-test", 1))
    return handler


async def _direct_call(sh: StepHandler, name: str, args: dict, result: str,
                       tc_id: str = "tc1") -> None:
    """Drive a tool call the way the native and MCP-over-native paths do —
    ``tool_calls_start`` + ``tool_complete`` carrying ``name`` verbatim."""
    await sh.handle("tool_calls_start", {
        "tools": [{"id": tc_id, "name": name, "hint": "", "args": ""}],
        "content": "",
        "tool_call_dicts": [{
            "id": tc_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    })
    await sh.handle("tool_complete", {"id": tc_id, "name": name, "result": result})


class TestFileWriteTriggerParity:
    """Managed-MCP and bare-native spellings register the same artifact."""

    async def test_mcp_prefixed_write_registers(self):
        db = get_db()
        sid = _mk_session(db)
        p = _WS / "notes" / f"{uuid.uuid4().hex[:6]}.md"
        await _direct_call(_handler(sid), "mcp__corsia__write_file",
                           {"path": str(p), "content": "x", "description": "a doc"},
                           f"Successfully wrote 1 bytes to {p}")
        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["type"] == "document"
        assert arts[0]["description"] == "a doc"

    async def test_bare_native_write_registers(self):
        db = get_db()
        sid = _mk_session(db)
        p = _WS / "notes" / f"{uuid.uuid4().hex[:6]}.md"
        await _direct_call(_handler(sid), "write_file",
                           {"path": str(p), "content": "x", "description": "a doc"},
                           f"Successfully wrote 1 bytes to {p}")
        arts = _artifacts(db, sid)
        assert len(arts) == 1
        assert arts[0]["description"] == "a doc"

    async def test_cc_managed_mcp_write_registers(self):
        db = get_db()
        parent = _mk_session(db)
        child, on_event = cc_child(db, parent)
        p = _WS / "working" / f"{uuid.uuid4().hex[:6]}.md"
        await on_event({
            "kind": "assistant_step", "text": "", "reasoning": "",
            "tool_uses": [{
                "id": "t1", "name": "mcp__corsia__write_file",
                "input": {"path": str(p), "content": "x"},
            }],
        })
        await on_event({"kind": "tool_result", "tool_use_id": "t1",
                        "content": f"File created successfully at: {p}",
                        "is_error": False})
        arts = _artifacts(db, child)
        assert len(arts) == 1

    async def test_mcp_prefixed_write_still_honors_native_error_text(self):
        """The mcp__corsia__-prefixed call executes the SAME native tool as
        a bare write_file call, so it must get the same result-text-based
        error/cancel suppression — not the CC-builtin is_error-only rule."""
        db = get_db()
        sid = _mk_session(db)
        p = str(_WS / f"{uuid.uuid4().hex[:6]}.md")
        await _direct_call(_handler(sid), "mcp__corsia__edit_file",
                           {"path": p, "old_string": "a", "new_string": "b"},
                           f"Error: File not found: {p}")
        assert _artifacts(db, sid) == []


class TestRegressionReproduction:
    """This is the exact shape that shipped broken: pre-fix, the observer's
    match tables only recognized bare/CC names, so the now-dominant
    mcp__corsia__-prefixed writer calls silently fell through."""

    async def test_mcp_prefixed_write_would_have_been_silently_dropped(self):
        db = get_db()
        sid = _mk_session(db)
        p = _WS / f"{uuid.uuid4().hex[:6]}.md"
        await _direct_call(_handler(sid), "mcp__corsia__write_file",
                           {"path": str(p), "content": "x"},
                           f"Successfully wrote 1 bytes to {p}")
        assert len(_artifacts(db, sid)) == 1


def test_writer_canonical_keys_are_a_subset_of_tool_catalog():
    """Guard: a future rename of write_file/edit_file/multi_edit in the
    catalog must surface here rather than silently desyncing the observer."""
    assert _NATIVE_TOOLS <= set(TOOL_CATALOG)
