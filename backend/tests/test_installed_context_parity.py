"""One adoption rule for static context sources on every harness (#2195).

A changed context file reaches a running conversation at the next checkpoint
install, model switch, or runtime-context boundary, and never because a Turn
started, a process relaunched, or the backend restarted.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

THREAD = "parity-thread"
HARNESS_MODELS = {
    "native": "anthropic/claude-test",
    "claude-code": "claude-code/sonnet",
    "codex": "codex/luna",
}


@pytest.fixture()
def parity(isolated_db, tmp_path, monkeypatch):
    """A workspace thread whose bootloader file carries one parity token."""
    from infra.database import get_db

    workspace = tmp_path / "workspace"
    (workspace / "context").mkdir(parents=True)
    (workspace / "context" / "AGENTS.md").write_text("Global rule.", encoding="utf-8")
    note = tmp_path / "PARITY.md"
    note.write_text("Parity marker: ALPHA", encoding="utf-8")
    monkeypatch.setattr("infra.config.WORKSPACE", workspace)
    db = get_db()
    db.execute(
        "INSERT INTO threads (id,user_id,title,model,bootloader_files,"
        "next_message_seq,created_at,updated_at) VALUES (?,?,?,?,?,4,'t0','t0')",
        (THREAD, "steve", "Parity", HARNESS_MODELS["native"], json.dumps([str(note)])),
    )
    for seq in range(1, 4):
        db.execute(
            "INSERT INTO messages (id,thread_id,role,content,created_at,seq) "
            "VALUES (?,?,?,'body','t0',?)",
            (f"m{seq}", THREAD, "user" if seq == 1 else "assistant", seq),
        )
    db.commit()
    db.close()
    return SimpleNamespace(workspace=workspace, note=note)


def _db():
    from infra.database import get_db

    return get_db()


def _set_model(model: str, **fields) -> None:
    db = _db()
    try:
        assignments = ",".join(f"{name}=?" for name in fields)
        db.execute(
            f"UPDATE threads SET model=?{',' if fields else ''}{assignments} WHERE id=?",
            (model, *fields.values(), THREAD),
        )
        db.commit()
    finally:
        db.close()


def _collect(model: str):
    from core.dynamic_context_registry import collect_context_sources
    from infra.config import WORKSPACE

    db = _db()
    try:
        return collect_context_sources(
            thread_id=THREAD, project_id=None, workspace=WORKSPACE, db=db,
            selected_model=model,
        )
    finally:
        db.close()


def _text(collection) -> str:
    from core.dynamic_context_registry import provider_context_prompt

    return provider_context_prompt(collection.sources)


def _prepared(harness: str, collection):
    """The prepared shape each harness hands the dispatch writer."""
    if harness == "native":
        return SimpleNamespace(installed=collection.installed)
    reconstruction = SimpleNamespace(installed=collection.installed)
    if harness == "claude-code":
        return SimpleNamespace(mode="cold", seed_reconstruction=reconstruction)
    return SimpleNamespace(mode="cold", reconstruction=reconstruction)


def _dispatch(harness: str, model: str):
    from core.installed_context import persist_prepared_installed_context

    collection = _collect(model)
    persist_prepared_installed_context(THREAD, _prepared(harness, collection))
    return collection


def _install_checkpoint(checkpoint_id: str, model: str, *, trigger="manual") -> None:
    from core.checkpoint_types import CheckpointRequest
    from infra.checkpoints import capture, create_running, install

    db = _db()
    try:
        through = db.execute(
            "SELECT MAX(seq) FROM messages WHERE thread_id=?", (THREAD,),
        ).fetchone()[0]
        previous = db.execute(
            "SELECT id FROM checkpoints WHERE thread_id=? AND status='installed' "
            "ORDER BY generation_order DESC LIMIT 1", (THREAD,),
        ).fetchone()
        request = CheckpointRequest(
            id=checkpoint_id, thread_id=THREAD, history_epoch=0,
            previous_generation_id=previous["id"] if previous else None,
            through_seq=through,
            through_message_id=db.execute(
                "SELECT id FROM messages WHERE thread_id=? AND seq=?",
                (THREAD, through),
            ).fetchone()["id"],
            trigger=trigger, mode="warm", source_harness="native",
            source_model=model, writer_harness="native", writer_model=model,
            originating_turn_id="turn-1", first_included_seq=1, started_at="t1",
        )
        create_running(request, db=db)
        capture(checkpoint_id, raw_result="raw", usage_json="{}",
                captured_at="t2", db=db)
        install(checkpoint_id, installed_at="t3", db=db)
        db.commit()
    finally:
        db.close()


@pytest.mark.parametrize("harness", sorted(HARNESS_MODELS))
def test_edit_waits_for_the_next_checkpoint_install(parity, harness):
    model = HARNESS_MODELS[harness]
    _set_model(model)
    installed = _text(_dispatch(harness, model))
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")

    # A new Turn, a CC relaunch, a Codex cold start, or a backend restart all
    # re-run the collector against the same database.
    relaunched = _text(_dispatch(harness, model))
    assert relaunched == installed
    assert "ALPHA" in relaunched and "BETA" not in relaunched

    _install_checkpoint("cp-1", model)
    adopted = _text(_dispatch(harness, model))
    assert "BETA" in adopted and "ALPHA" not in adopted

    parity.note.write_text("Parity marker: GAMMA", encoding="utf-8")
    assert _text(_collect(model)) == adopted


@pytest.mark.parametrize("harness", sorted(HARNESS_MODELS))
def test_model_switch_adopts_current_sources(parity, harness):
    model = HARNESS_MODELS[harness]
    _set_model(model)
    _dispatch(harness, model)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")

    _set_model(f"{model}-next", execution_epoch=1)
    switched = _text(_dispatch(harness, f"{model}-next"))
    assert "BETA" in switched

    parity.note.write_text("Parity marker: GAMMA", encoding="utf-8")
    assert _text(_collect(f"{model}-next")) == switched


@pytest.mark.parametrize("harness", sorted(HARNESS_MODELS))
def test_runtime_context_boundary_adopts_current_sources(parity, harness):
    model = HARNESS_MODELS[harness]
    _set_model(model)
    _dispatch(harness, model)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")

    # Conductor designation advances only the runtime-context epoch.
    _set_model(model, runtime_context_epoch=1)
    assert "BETA" in _text(_collect(model))


def test_model_switch_receipt_binds_the_destination_runtime_epoch(parity):
    source, destination = HARNESS_MODELS["claude-code"], HARNESS_MODELS["codex"]
    _set_model(source)
    _dispatch("claude-code", source)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    _set_model(source, pending_model=destination, pending_execution_epoch=1)
    _install_checkpoint("cp-switch", destination, trigger="model_switch")
    parity.note.write_text("Parity marker: GAMMA", encoding="utf-8")

    # activate_ready_switch commits the destination with runtime epoch + 1.
    # Its first dispatch is the first delivery after the install, so it
    # freezes what it sends and binds the receipt's unbound runtime epoch.
    _set_model(destination, execution_epoch=1, pending_model=None,
               pending_execution_epoch=None, runtime_context_epoch=1)
    first = _text(_dispatch("codex", destination))
    assert "GAMMA" in first
    parity.note.write_text("Parity marker: DELTA", encoding="utf-8")
    assert _text(_dispatch("codex", destination)) == first

    _set_model(destination, runtime_context_epoch=2)
    assert "DELTA" in _text(_collect(destination))


def test_legacy_thread_without_sources_freezes_at_first_dispatch(parity):
    from core.agent_context_sources import current_responsibility_map_snapshot

    model = HARNESS_MODELS["native"]
    db = _db()
    try:
        legacy = current_responsibility_map_snapshot(THREAD, db, model=model)
        legacy.pop("runtime_context_epoch")
        db.execute(
            "UPDATE messages SET metadata=? WHERE id='m1'",
            (json.dumps({"responsibility_map_snapshot": legacy}),),
        )
        db.commit()
    finally:
        db.close()
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")

    first = _text(_dispatch("native", model))
    assert "BETA" in first
    parity.note.write_text("Parity marker: GAMMA", encoding="utf-8")
    assert _text(_collect(model)) == first


def test_dispatch_logs_one_distinct_adoption_reason(parity, caplog):
    model = HARNESS_MODELS["native"]
    caplog.set_level(logging.INFO, logger="core.installed_context")

    def reasons() -> list[str]:
        found = [
            record.getMessage().split("adoption=")[1].split()[0]
            for record in caplog.records
            if "installed_context.dispatch" in record.getMessage()
        ]
        caplog.clear()
        return found

    _dispatch("native", model)
    assert reasons() == ["current-no-anchor"]
    _dispatch("native", model)
    assert reasons() == ["retained"]
    _set_model(model, runtime_context_epoch=1)
    _dispatch("native", model)
    assert reasons() == ["key-mismatch"]
    _dispatch("native", model)
    assert reasons() == ["retained"]


def test_live_provider_resume_logs_but_never_writes(parity, caplog):
    from core.installed_context import persist_prepared_installed_context

    model = HARNESS_MODELS["claude-code"]
    caplog.set_level(logging.INFO, logger="core.installed_context")
    collection = _collect(model)
    prepared = SimpleNamespace(
        mode="provider_resume",
        seed_reconstruction=SimpleNamespace(installed=collection.installed),
    )

    assert not persist_prepared_installed_context(THREAD, prepared)
    assert "mode=provider_resume adoption=current-no-anchor written=False" in (
        caplog.text
    )


def test_installed_source_bytes_stay_out_of_published_metadata(parity):
    from api.thread_paging import _parse_message_metadata
    from core.installed_context import INSTALLED_SOURCES
    from infra.send_broadcast import InputBroadcastRow, project_input_message

    model = HARNESS_MODELS["native"]
    _dispatch("native", model)
    _install_checkpoint("cp-1", model)
    _dispatch("native", model)
    rows = _boundary_rows()
    assert len(rows) == 2
    for row in rows:
        stored = json.loads(row["metadata"])
        assert "sources" not in stored["responsibility_map_snapshot"]
        # Only the current boundary keeps bytes; the superseded one drops them.
        assert (INSTALLED_SOURCES in stored) == (row["id"] != "m1")
        assert any(
            "ALPHA" in (source["prompt_content"] or "")
            for source in stored.get(INSTALLED_SOURCES, [])
        ) == (row["id"] != "m1")
        rest = [{"metadata": row["metadata"]}]
        _parse_message_metadata(rest)
        pushed = project_input_message(InputBroadcastRow(
            message_id=row["id"], content="c", metadata=stored, created_at="t",
            role="system", delivery_intent=None,
        ))
        for published in (rest[0]["metadata"], pushed["metadata"]):
            assert INSTALLED_SOURCES not in published
            assert "ALPHA" not in json.dumps(published)


def _boundary_rows():
    db = _db()
    try:
        return db.execute(
            "SELECT id,metadata FROM messages "
            "WHERE id IN ('m1','checkpoint-receipt:cp-1') ORDER BY id"
        ).fetchall()
    finally:
        db.close()


def _receipt() -> dict:
    return json.loads(
        next(row for row in _boundary_rows() if row["id"] != "m1")["metadata"]
    )


def test_agent_facts_publish_the_map_not_the_delivery(parity):
    collection = _collect(HARNESS_MODELS["native"])
    snapshot = collection.agent_facts["responsibility_map_snapshot"]
    assert "sources" not in snapshot
    assert "ALPHA" not in json.dumps(collection.agent_facts)
    # The delivered bytes ride only on the private delivery.
    assert any(
        "ALPHA" in (row["prompt_content"] or "")
        for row in collection.installed.snapshot["sources"]
    )


def test_role_facts_name_the_installed_roles_until_install(parity):
    """Provenance agrees with the retained role rows, not the live flag."""
    roles_dir = parity.workspace / "context" / "roles" / "agent"
    roles_dir.mkdir(parents=True)
    (roles_dir / "OPERATE.md").write_text("Agent role marker.", encoding="utf-8")
    model = HARNESS_MODELS["native"]

    def roles(collection):
        rows = [s for s in collection.sources if s.origin_section == "role"
                and s.metadata.get("provider") == "role_context"]
        return collection.agent_facts["receives_agent_role"], bool(rows)

    assert roles(_dispatch("native", model)) == (False, False)
    _set_model(model, agent_enabled=1)
    assert roles(_dispatch("native", model)) == (False, False)
    _install_checkpoint("cp-1", model)
    assert roles(_dispatch("native", model)) == (True, True)
    assert roles(_collect(model)) == (True, True)


def test_install_receipt_holds_the_map_until_the_first_dispatch(parity):
    from core.installed_context import INSTALLED_SOURCES

    model = HARNESS_MODELS["native"]
    _dispatch("native", model)
    _install_checkpoint("cp-1", model)
    assert INSTALLED_SOURCES not in _receipt()

    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    first = _text(_dispatch("native", model))
    assert "BETA" in first
    assert any(
        "BETA" in (row["prompt_content"] or "") for row in _receipt()[INSTALLED_SOURCES]
    )


def test_dispatch_retains_exactly_what_it_delivered(parity):
    """A disk edit between collection and persist is not what was delivered."""
    from core.installed_context import persist_prepared_installed_context

    model = HARNESS_MODELS["native"]
    collection = _collect(model)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")

    assert persist_prepared_installed_context(THREAD, _prepared("native", collection))
    assert _text(_collect(model)) == _text(collection)
    assert "ALPHA" in _text(collection)


def test_retained_turn_reads_no_context_file(parity, monkeypatch):
    model = HARNESS_MODELS["native"]
    _dispatch("native", model)

    def no_reads(*_args, **_kwargs):
        raise AssertionError("a retained Turn read context files")

    monkeypatch.setattr("core.dynamic_context_registry._static_sources", no_reads)
    retained = _dispatch("native", model)
    assert retained.installed.adoption == "retained"
    assert not retained.installed.pending
    assert "ALPHA" in _text(retained)


def test_install_between_collection_and_persist_keeps_the_new_boundary(
    parity, caplog,
):
    from core.installed_context import (
        INSTALLED_SOURCES,
        persist_prepared_installed_context,
    )

    model = HARNESS_MODELS["native"]
    _dispatch("native", model)
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    _set_model(model, runtime_context_epoch=1)
    stale = _collect(model)
    assert stale.installed.pending
    _install_checkpoint("cp-1", model)
    receipt = _receipt()
    caplog.set_level(logging.INFO, logger="core.installed_context")

    assert not persist_prepared_installed_context(THREAD, _prepared("native", stale))
    assert "written=False reason=boundary-moved" in caplog.text
    assert _receipt() == receipt
    assert INSTALLED_SOURCES not in receipt

    parity.note.write_text("Parity marker: GAMMA", encoding="utf-8")
    adopted = _dispatch("native", model)
    assert adopted.installed.boundary_id == "checkpoint-receipt:cp-1"
    assert "GAMMA" in _text(adopted)


def test_key_mismatch_replaces_and_a_complete_snapshot_is_never_rewritten(parity):
    from core.installed_context import INSTALLED_SOURCES

    model = HARNESS_MODELS["native"]
    _dispatch("native", model)
    _install_checkpoint("cp-1", model)
    _dispatch("native", model)
    frozen = _receipt()
    _dispatch("native", model)
    assert _receipt() == frozen

    # A runtime-context boundary crossed without a checkpoint replaces the
    # receipt's snapshot with what the crossing dispatch delivers.
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    _set_model(model, runtime_context_epoch=1)
    _dispatch("native", model)
    replaced = _receipt()
    assert replaced["responsibility_map_snapshot"]["runtime_context_epoch"] == 1
    assert any(
        "BETA" in (row["prompt_content"] or "") for row in replaced[INSTALLED_SOURCES]
    )


@pytest.mark.asyncio
async def test_native_real_builder_holds_file_bytes_until_install(parity, monkeypatch):
    """The real ContextBuilder renders system, project, and thread files from
    the install boundary, not from disk on each Turn."""
    from core.context_builder import ContextBuilder
    from core.installed_context import persist_prepared_installed_context
    from core.native_context_prefix import build_real_send_prefix

    from tests.project_ref_helpers import project_ref, select_workspace

    select_workspace(monkeypatch, parity.workspace)
    project_dir = parity.workspace / "projects" / "demo"
    (project_dir / "context").mkdir(parents=True)
    project_file = project_dir / "context" / "PROJECT.md"
    project_file.write_text("Project marker: P-ALPHA", encoding="utf-8")
    (project_dir / "context" / "COORDINATION-MODEL.md").write_text("Model.")
    system_file = parity.workspace / "context" / "AGENTS.md"
    system_file.write_text("System marker: S-ALPHA", encoding="utf-8")
    db = _db()
    db.execute(
        "INSERT INTO projects (id,name,path,created_at,updated_at) "
        "VALUES ('p1','Demo',?,'t0','t0')", (project_ref("demo"),),
    )
    db.execute("UPDATE threads SET project_id='p1' WHERE id=?", (THREAD,))
    db.commit()
    db.close()
    monkeypatch.setattr("llm.capabilities.get_model_context_window", lambda _m: 200000)
    agent = SimpleNamespace(
        context=ContextBuilder(parity.workspace),
        tools=SimpleNamespace(get_definitions=lambda: []),
        memory_window=100, max_tokens=1024,
    )

    async def build() -> str:
        prefix, info = await build_real_send_prefix(
            agent=agent, thread_id=THREAD, turn_model="anthropic/claude-test",
            fresh_context=True, project_id="p1",
            selected_model=HARNESS_MODELS["native"],
        )
        persist_prepared_installed_context(
            THREAD, SimpleNamespace(installed=info["installed_context"]),
        )
        return prefix[0]["content"]

    import core.dynamic_context_registry as registry

    reads: list[int] = []
    real_static = registry._static_sources

    def counted(*args, **kwargs):
        reads.append(1)
        return real_static(*args, **kwargs)

    monkeypatch.setattr(registry, "_static_sources", counted)
    first = await build()
    assert all(m in first for m in ("S-ALPHA", "P-ALPHA", "Parity marker: ALPHA"))
    system_file.write_text("System marker: S-BETA", encoding="utf-8")
    project_file.write_text("Project marker: P-BETA", encoding="utf-8")
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    assert await build() == first
    assert len(reads) == 1  # one read per boundary, none per retained Turn

    _install_checkpoint("cp-1", HARNESS_MODELS["native"])
    adopted = await build()
    assert len(reads) == 2
    assert all(m in adopted for m in ("S-BETA", "P-BETA", "Parity marker: BETA"))
    assert not any(m in adopted for m in ("S-ALPHA", "P-ALPHA", "marker: ALPHA"))


def test_claude_code_seed_is_byte_identical_across_relaunch(parity):
    from subagents.cc_context_seed import build_cc_context_reconstruction

    model = HARNESS_MODELS["claude-code"]
    _set_model(model)

    def seed() -> str:
        db = _db()
        try:
            return build_cc_context_reconstruction(
                thread_id=THREAD, base_seed="BASE", catalog_model_id=model,
                model_alias="sonnet", working_dir=None, project_id=None,
                is_subagent=False, provider_handle_opaque=True, db=db,
            )
        finally:
            db.close()

    launched = seed()
    from core.installed_context import persist_prepared_installed_context

    persist_prepared_installed_context(
        THREAD, SimpleNamespace(mode="cold", seed_reconstruction=launched),
    )
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    assert seed().seed_text == launched.seed_text

    _install_checkpoint("cp-1", model)
    assert "BETA" in seed().seed_text


def test_codex_cold_start_after_checkpoint_adopts(parity):
    from history.types import PortableHistoryProjection
    from subagents.codex_context_reconstruction import build_codex_context_reconstruction

    model = HARNESS_MODELS["codex"]
    _set_model(model)
    projection = PortableHistoryProjection(
        format_version=1, thread_id=THREAD, history_epoch=0,
        start_exclusive=None, sealed_boundary=None, turns=(),
        full_raw_replay_available=True,
    )

    def cold() -> str:
        db = _db()
        try:
            return build_codex_context_reconstruction(
                thread_id=THREAD, user_text="go", working_dir=None,
                project_id=None, model=model, model_context_window=200000,
                history_projection=projection, db=db,
            )
        finally:
            db.close()

    first = cold()
    from core.installed_context import persist_prepared_installed_context

    persist_prepared_installed_context(
        THREAD, SimpleNamespace(mode="cold", reconstruction=first),
    )
    parity.note.write_text("Parity marker: BETA", encoding="utf-8")
    assert "BETA" not in cold().task_text

    _install_checkpoint("cp-1", model)
    assert "BETA" in cold().task_text


@pytest.mark.parametrize(
    ("codex_thread_id", "expected"), [(None, ["delivery"]), ("codex-1", [])],
)
def test_codex_retains_only_what_a_cold_start_delivers(
    monkeypatch, codex_thread_id, expected,
):
    from subagents import codex_spawn_preflight as preflight

    reconstruction = SimpleNamespace(
        installed="delivery", task_text="task", turn_text="turn", history_items=(),
        checkpoint_text=None,
    )
    monkeypatch.setattr(
        preflight, "build_codex_reconstruction", lambda **_kw: reconstruction,
    )
    retained: list[object] = []
    monkeypatch.setattr(
        preflight, "persist_installed_delivery",
        lambda delivery, *, mode, db: retained.append(delivery),
    )

    payload = preflight.resolve_spawn_payload(
        thread_id=THREAD, text="hi", current_input_message_id=None,
        codex_thread_id=codex_thread_id, catalog_model_id="codex/luna",
        context_window=None, prepared_context=None, working_dir=None,
        project_id=None, db=None,
    )

    assert payload.reconstruction is reconstruction
    assert retained == expected


def test_restore_sources_remaps_only_legacy_workspace_rows_without_changing_bytes():
    from core.installed_context import restore_sources

    legacy_workspace = {
        "source_id": "native:system:bootstrap:context/AGENTS.md",
        "origin_section": "system",
        "label": "Workspace context: context/AGENTS.md",
        "stable_reference": "workspace://context/AGENTS.md",
        "freshness": "fresh",
        "inclusion": "included",
        "reason": "workspace context file",
        "owner_surface": "document-viewer",
        "prompt_content": "## context/AGENTS.md\n\nExact bytes stay intact.\n",
    }
    runtime_envelope = {
        **legacy_workspace,
        "source_id": "native:system:core",
        "prompt_content": "Runtime envelope remains system.\n",
    }

    restored = restore_sources([legacy_workspace, runtime_envelope])

    assert legacy_workspace["origin_section"] == "system"
    assert restored[0].origin_section == "workspace"
    assert restored[0].prompt_content == legacy_workspace["prompt_content"]
    assert restored[1].origin_section == "system"
    assert restored[1].prompt_content == runtime_envelope["prompt_content"]
