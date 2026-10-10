"""Scoped checkpoint tails produce byte-identical provider payloads and snapshot IDs.

For every legal cut in each history shape, the Native, Claude Code, and Codex
renderings of a scoped tail, its checkpoint prompt, and the effective-context
snapshot ID built from them must equal the rendering of the same tail read from
seq 0.
"""

from __future__ import annotations

import json

import pytest

from tests.test_history_scoped_source import SHAPES, THREAD, _legal_boundaries


def _tail(db, boundary, *, full_read: bool, monkeypatch):
    import history.projection as projection

    with monkeypatch.context() as patch:
        if full_read:
            real = projection._read_source
            patch.setattr(projection, "_read_source", lambda d, t, _s: real(d, t, None))
        return projection.render_portable_history(
            THREAD, db=db, start_exclusive=boundary, include_unfinished_turn=True,
        )


def _renderings(projection, boundary) -> dict[str, str]:
    from core.context_replay import ReplaySelection, checkpoint_prompt
    from core.effective_context import build_effective_context_snapshot
    from history.adapters import to_native_history
    from subagents.cc_context_reconstruction import cc_snapshot_history
    from subagents.codex_context_reconstruction import (
        _portable_history_source,
        codex_inject_items,
    )

    histories = {
        "native": to_native_history(projection),
        "claude-code": cc_snapshot_history(projection, cc_session_id="cc-parity"),
        "codex": list(codex_inject_items(projection)),
    }
    selection = ReplaySelection(
        "checkpoint_tail", projection.history_epoch, projection.sealed_boundary,
        projection, checkpoint_generation_id="cp-parity",
        checkpoint_boundary=boundary, checkpoint_json='{"summary":"parity"}',
    )
    rendered = {name: json.dumps(items, sort_keys=True) for name, items in histories.items()}
    rendered["checkpoint_prompt"] = checkpoint_prompt(selection)
    for harness, items in histories.items():
        rendered[f"snapshot:{harness}"] = build_effective_context_snapshot(
            thread_id=THREAD, reason="parity", scope_facts=None, agent_facts=None,
            harness_model_facts=None, cwd=None, source_root=None, sources=[],
            history=list(items),
            provider_payload={"portable_history": _portable_history_source(projection)},
            delivery_harnesses=(harness,),
        ).snapshot_id
    return rendered


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_scoped_tail_payloads_and_snapshot_ids_match_seq0_read(monkeypatch, shape):
    db = SHAPES[shape]().db
    compared = 0
    for boundary in _legal_boundaries(db):
        try:
            reference = _tail(db, boundary, full_read=True, monkeypatch=monkeypatch)
        except Exception:  # noqa: BLE001 - failures are covered by the equivalence suite
            continue
        scoped = _tail(db, boundary, full_read=False, monkeypatch=monkeypatch)
        assert _renderings(scoped, boundary) == _renderings(reference, boundary), boundary
        compared += 1
    assert compared
