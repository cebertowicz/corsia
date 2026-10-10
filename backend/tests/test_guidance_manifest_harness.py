"""Per-harness proof: a Turn's usage names the guidance version it delivered,
even after a checkpoint dropped the bytes it was installed with (#2274)."""

from __future__ import annotations

from contextlib import closing
from types import SimpleNamespace

import pytest

from core.guidance_manifest import ManifestHeader, git_blob_oid, record_turn_manifest
from test_installed_context_parity import (  # noqa: F401 - fixture import
    HARNESS_MODELS,
    THREAD,
    _db,
    _install_checkpoint,
    _set_model,
    parity,
)

HARNESSES = ("native", "claude-code-cold", "claude-code-warm", "codex")


def _model(harness: str) -> str:
    return HARNESS_MODELS[harness.removesuffix("-cold").removesuffix("-warm")]


async def _prepared(harness: str, workspace):
    """What each harness's dispatch hands the writer, built by its real builder."""
    model = _model(harness)
    with closing(_db()) as db:
        if harness == "native":
            from core.context_builder import ContextBuilder
            from core.turn_context import prepare_turn_context

            agent = SimpleNamespace(
                context=ContextBuilder(workspace),
                tools=SimpleNamespace(get_definitions=lambda: []),
                memory_window=100, max_tokens=1024,
            )
            installed: list = []
            snapshot: list = []
            await prepare_turn_context(
                agent, THREAD, "go", model, True, None, "web", THREAD, None,
                model_context_window=200000, tool_definitions=[], selected_model=model,
                installed_out=installed, snapshot_out=snapshot,
            )
            return SimpleNamespace(installed=installed[0], snapshot=snapshot[0])
        if harness == "codex":
            from history.types import PortableHistoryProjection
            from subagents.codex_context_reconstruction import (
                build_codex_context_reconstruction,
            )

            projection = PortableHistoryProjection(
                format_version=1, thread_id=THREAD, history_epoch=0,
                start_exclusive=None, sealed_boundary=None, turns=(),
                full_raw_replay_available=True,
            )
            return SimpleNamespace(mode="cold", reconstruction=(
                build_codex_context_reconstruction(
                    thread_id=THREAD, user_text="go", working_dir=None,
                    project_id=None, model=model, model_context_window=200000,
                    history_projection=projection, db=db,
                )
            ))
        from subagents.cc_context_frame import build_cc_user_frame_reconstruction
        from subagents.cc_context_seed import build_cc_context_reconstruction

        seed = build_cc_context_reconstruction(
            thread_id=THREAD, base_seed="BASE", catalog_model_id=model,
            model_alias="sonnet", working_dir=None, project_id=None,
            is_subagent=False, provider_handle_opaque=True, db=db,
        )
        frame = build_cc_user_frame_reconstruction(
            thread_id=THREAD, user_text="go", catalog_model_id=model,
            model_alias="sonnet", working_dir=None, project_id=None,
            is_subagent=False, provider_handle_opaque=True, db=db,
        )
        mode = "provider_resume" if harness == "claude-code-warm" else "cold"
        return SimpleNamespace(
            mode=mode, seed_reconstruction=seed, frame_reconstruction=frame,
        )


async def _turn(harness: str, workspace, turn_id: str) -> str:
    """Dispatch one Turn the way ``_dispatch_prepared`` does, then bill one call."""
    from core.installed_context import persist_prepared_installed_context
    from infra.turn_usage_receipts import (
        project_turn_usage_receipt,
        store_turn_usage_receipt,
    )

    with closing(_db()) as db:
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) VALUES (?,?,0,?,'completed','t0','b')",
            (turn_id, THREAD, _model(harness)),
        )
        db.commit()
    prepared = await _prepared(harness, workspace)
    if harness == "claude-code-warm":
        # The live process was launched from its seed; the frame is this Turn.
        persist_prepared_installed_context(
            THREAD, SimpleNamespace(mode="cold", seed_reconstruction=prepared.seed_reconstruction),
        )
    persist_prepared_installed_context(THREAD, prepared)
    model = _model(harness)
    listed = record_turn_manifest(
        THREAD, turn_id, prepared,
        ManifestHeader.for_selector(
            harness=harness.removesuffix("-cold").removesuffix("-warm"),
            selector=model, expected_reported_model=None, transport_model=model,
        ),
    )
    assert listed is not None
    with closing(_db()) as db:
        identity, _projected, _binding = store_turn_usage_receipt(
            db, thread_id=THREAD, turn_id=turn_id, usage_key="provider-call:1",
            model=model, measured={"prompt_tokens": 5}, ttft_ms=None,
            source="conversation", now_iso="t1",
        )
        project_turn_usage_receipt(db, turn_id, identity)
    return listed


def _delivered_oid(turn_id: str, path) -> set[str | None]:
    """The one query: this Turn's usage -> its list -> the file's version."""
    with closing(_db()) as db:
        return {
            row[0] for row in db.execute(
                """SELECT json_extract(entry.value, '$.git_blob_oid')
                   FROM llm_usage AS usage
                   JOIN context_manifests AS manifest
                     ON manifest.id = usage.context_manifest_id,
                   json_each(manifest.entries_json) AS entry
                   WHERE usage.turn_id = ?
                     AND json_extract(entry.value, '$.stable_reference') = ?""",
                (turn_id, str(path.resolve())),
            ).fetchall()
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", HARNESSES)
async def test_usage_names_the_installed_version_across_a_checkpoint(parity, harness):
    model = _model(harness)
    _set_model(model)
    alpha = git_blob_oid(parity.note.read_bytes())

    first = await _turn(harness, parity.workspace, "turn-1")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    beta = git_blob_oid(parity.note.read_bytes())
    retained = await _turn(harness, parity.workspace, "turn-2")
    _install_checkpoint("cp-1", model)
    adopted = await _turn(harness, parity.workspace, "turn-3")

    # Until the install, the Turn still has (and reports) what it was installed with.
    assert retained == first
    assert adopted != first
    assert _delivered_oid("turn-1", parity.note) == {alpha}
    assert _delivered_oid("turn-2", parity.note) == {alpha}
    assert _delivered_oid("turn-3", parity.note) == {beta}
    with closing(_db()) as db:
        statuses = {
            row[0] for row in db.execute(
                "SELECT context_manifest_status FROM active_turns WHERE thread_id=?",
                (THREAD,),
            )
        }
    assert statuses == {"recorded"}
