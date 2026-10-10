"""The context sheet's pending-edit flag: recorded hash vs the file on disk."""

from __future__ import annotations

import os

import pytest
from test_installed_context_parity import (  # noqa: F401  (parity is a fixture)
    HARNESS_MODELS, _collect, _dispatch, _set_model, parity,
)

from core.pending_edit_marker import EDITED_SINCE_INSTALL, annotate_pending_edits


def _flag(collection, path) -> bool | None:
    usage = {"context_snapshot": {"source_inventory": [
        {"source_id": s.source_id, "metadata": s.metadata}
        for s in collection.sources
    ]}}
    annotate_pending_edits(usage, collection)
    rows = [
        row for row in usage["context_snapshot"]["source_inventory"]
        if row["metadata"].get("path") == str(path)
    ]
    assert len(rows) == 1
    return rows[0].get(EDITED_SINCE_INSTALL)


def _install(harness):
    model = HARNESS_MODELS[harness]
    _set_model(model)
    _dispatch(harness, model)
    return model


@pytest.mark.parametrize("harness", sorted(HARNESS_MODELS))
def test_hash_equal_shows_no_marker(parity, harness):
    model = _install(harness)
    assert _flag(_collect(model), parity.note) is False


@pytest.mark.parametrize("harness", sorted(HARNESS_MODELS))
def test_hash_differs_flags_the_edit_on_every_harness(parity, harness):
    """Native too: it renders from the retained rows, so its edit is pending."""
    model = _install(harness)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    collection = _collect(model)
    assert collection.installed.adoption == "retained"
    assert _flag(collection, parity.note) is True


def test_edit_reverted_to_the_original_bytes_shows_no_marker(parity):
    model = _install("native")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    parity.note.write_text("Parity marker: ALPHA", encoding="utf-8")
    assert _flag(_collect(model), parity.note) is False


def test_file_changed_during_the_read_shows_no_marker(parity, monkeypatch):
    model = _install("native")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    collection = _collect(model)
    real = os.stat
    calls = {"n": 0}

    def moving_stat(path, *a, **kw):
        result = real(path, *a, **kw)
        if str(path) == str(parity.note):
            calls["n"] += 1
            if calls["n"] == 2:  # the after-read stat sees a newer mtime
                return os.stat_result(
                    (result.st_mode, result.st_ino, result.st_dev, result.st_nlink,
                     result.st_uid, result.st_gid, result.st_size,
                     int(result.st_atime), int(result.st_mtime) + 1,
                     int(result.st_ctime), result.st_atime, result.st_mtime + 1,
                     result.st_ctime)
                )
        return result

    monkeypatch.setattr("pathlib.Path.stat", lambda self, **kw: moving_stat(self, **kw))
    assert _flag(collection, parity.note) is None
    assert calls["n"] == 2


def test_deleted_or_unreadable_file_shows_no_marker(parity):
    model = _install("native")
    parity.note.unlink()
    assert _flag(_collect(model), parity.note) is None


@pytest.mark.parametrize("status", ["changed_since_read", "unreadable"])
def test_unknown_recorded_version_shows_no_marker(parity, status):
    model = _install("native")
    collection = _collect(model)
    for source in collection.sources:
        if (source.metadata or {}).get("path") == str(parity.note):
            source.raw_version = {"status": status}
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    assert _flag(collection, parity.note) is None


def test_row_without_a_recorded_version_shows_no_marker(parity):
    model = _install("native")
    collection = _collect(model)
    for source in collection.sources:
        source.raw_version = None
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    assert _flag(collection, parity.note) is None


def test_rows_read_fresh_in_this_preview_get_no_marker(parity):
    model = HARNESS_MODELS["native"]
    _set_model(model)
    collection = _collect(model)  # never dispatched: read from disk now
    assert collection.installed.adoption != "retained"
    assert _flag(collection, parity.note) is None


def test_one_read_per_file_per_preview(parity, monkeypatch):
    model = _install("native")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    collection = _collect(model)
    reads: list[str] = []
    real = type(parity.note).read_bytes

    def counted(self):
        reads.append(str(self))
        return real(self)

    monkeypatch.setattr("pathlib.Path.read_bytes", counted)
    assert _flag(collection, parity.note) is True
    assert reads.count(str(parity.note)) == 1


def test_marker_leaves_delivered_hashes_and_manifest_unchanged(parity):
    from core.guidance_manifest import _entry

    model = _install("native")
    before = _collect(model)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    after = _collect(model)
    delivered = lambda c: {  # noqa: E731
        s.source_id: _entry(s, model=model) for s in c.sources if s.prompt_content
    }
    base = delivered(before)
    assert delivered(after) == base  # delivered_sha256, raw hashes, version kind
    usage = {"context_snapshot": {"source_inventory": []}}
    assert annotate_pending_edits(usage, after) is usage
    assert delivered(after) == base


def test_preview_response_carries_the_flag_without_changing_the_snapshot(parity):
    """The flag rides the one preview payload and is outside the snapshot hash."""
    from core.context_preview_projection import PreviewInputs, preview_usage_for_next_turn

    model = _install("native")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    collection = _collect(model)
    inputs = PreviewInputs(
        thread_id="parity-thread", provider="native", model=model, klass="native",
        is_subagent=False, project_id=None, collection=collection,
        current_user_content="", canonical_tool_definitions=None,
        frozen_tool_definitions=(), history_messages=0,
        preflight={
            "status": "allowed", "continuity_mode": "full",
            "model_context_window": 32000, "total_tokens": 100,
        },
        system_tokens=0, tool_def_tokens=0, history_tokens=0, user_tokens=0,
        checkpoint_eligible=False, checkpoint_ineligible_reason=None,
        discovered_context_window=None,
    )
    usage = preview_usage_for_next_turn(inputs)
    rows = {
        r["metadata"].get("path"): r
        for r in usage["context_snapshot"]["source_inventory"]
    }
    assert rows[str(parity.note)][EDITED_SINCE_INSTALL] is True
    system = rows[str(parity.workspace / "context" / "AGENTS.md")]
    assert system[EDITED_SINCE_INSTALL] is False
    parity.note.write_text("Parity marker: ALPHA", encoding="utf-8")
    reverted = preview_usage_for_next_turn(inputs)
    assert reverted["content_hash"] == usage["content_hash"]
