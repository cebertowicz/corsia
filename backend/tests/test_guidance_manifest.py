"""Which guidance, at which version, each Turn delivered (#2274)."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from core.effective_context import ContextSourceEntry, build_effective_context_snapshot
from core.guidance_manifest import (
    ManifestHeader,
    build_guidance_manifest,
    git_blob_oid,
    prepared_snapshot,
    record_guidance_load,
    record_turn_manifest,
    stamp_raw_versions,
)
from infra.schema import SCHEMA_SQL
from tests.schema_rewind import rewind_to_v88_policy

HEADER = ManifestHeader("claude-code", "claude-code/sonnet", "claude-sonnet-5", "catalog")


def _file_source(path, *, origin="project", source_id=None) -> ContextSourceEntry:
    return ContextSourceEntry(
        source_id=source_id or f"{origin}:{path.name}", origin_section=origin,
        label=path.name, stable_reference=str(path), freshness="fresh",
        inclusion="included", reason="file", owner_surface="document-viewer",
        prompt_content=f"## {path.name}\n\n{path.read_text(encoding='utf-8')}",
        metadata={"path": str(path)},
    )


def _row(source_id, origin, text, inclusion="included") -> ContextSourceEntry:
    return ContextSourceEntry(
        source_id=source_id, origin_section=origin, label=source_id,
        stable_reference=source_id, freshness="fresh", inclusion=inclusion,
        reason="row", owner_surface="x", prompt_content=text,
    )


def _snapshot(sources, *, history=None, user="hi"):
    return build_effective_context_snapshot(
        thread_id="thread-1", reason="test", scope_facts={}, agent_facts={},
        harness_model_facts={"harness": "claude-code", "model": "claude-code/sonnet"},
        cwd=None, source_root=None, sources=sources, history=history,
        current_user_content=user,
    )


@pytest.fixture
def guide(tmp_path):
    path = tmp_path / "PROJECT.md"
    path.write_text("Project rule: ALPHA\n", encoding="utf-8")
    return path


def test_blob_oid_is_the_one_git_computes(guide):
    expected = subprocess.run(
        ["git", "hash-object", str(guide)], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert git_blob_oid(guide.read_bytes()) == expected


def test_same_guidance_shares_one_list_whatever_the_history(guide):
    source = _file_source(guide)
    stamp_raw_versions([source])
    first = build_guidance_manifest(_snapshot([source], history=[]), HEADER)
    later = build_guidance_manifest(
        _snapshot([source], history=[{"role": "user", "content": "earlier"}], user="next"),
        HEADER,
    )
    assert first.id == later.id
    (entry,) = first.entries
    assert entry["version_kind"] == "git_blob"
    assert entry["git_blob_oid"] == git_blob_oid(guide.read_bytes())
    assert entry["prompt_tokens"] > 0


def test_an_edit_or_a_new_target_makes_a_new_list(guide):
    source = _file_source(guide)
    stamp_raw_versions([source])
    before = build_guidance_manifest(_snapshot([source]), HEADER)
    guide.write_text("Project rule: BETA\n", encoding="utf-8")
    edited = _file_source(guide)
    stamp_raw_versions([edited])
    after = build_guidance_manifest(_snapshot([edited]), HEADER)
    assert after.id != before.id
    other = ManifestHeader("codex", "codex/luna", "gpt-5.6-luna", "catalog")
    assert build_guidance_manifest(_snapshot([source]), other).id != before.id


def test_turn_varying_rows_stay_out_and_the_checkpoint_stays_in():
    sources = [
        _row("user-frame", "user", "hi"),
        _row("model", "model", "model facts"),
        _row("tools", "tools", "tool ledger"),
        _row("transcript", "history", "old turns"),
        _row("history:portable-checkpoint", "history", "checkpoint narrative"),
        _row("dynamic:skills-index", "dynamic", "skills"),
        _row("role:x", "role", "role text", inclusion="omitted"),
        _row("thread:y", "thread", "thread component", inclusion="proof"),
    ]
    manifest = build_guidance_manifest(_snapshot(sources), HEADER)
    by_id = {entry["source_id"]: entry for entry in manifest.entries}
    assert set(by_id) == {
        "history:portable-checkpoint", "dynamic:skills-index", "thread:y",
    }
    assert by_id["dynamic:skills-index"]["version_kind"] == "generated"


def test_raw_version_stays_out_of_the_snapshot_hash_and_survives_install(guide):
    from core.installed_context import restore_sources, serialize_sources

    plain = _file_source(guide)
    stamped = _file_source(guide)
    stamp_raw_versions([stamped])
    assert stamped.raw_version["status"] == "read"
    assert _snapshot([plain]).content_hash == _snapshot([stamped]).content_hash
    assert "raw_version" not in stamped.to_inventory_dict()

    (restored,) = restore_sources(serialize_sources([stamped]))
    assert restored.raw_version == stamped.raw_version


def test_versions_nobody_can_vouch_for_are_named(guide):
    changed = _file_source(guide)
    guide.write_text("edited between read and hash", encoding="utf-8")
    stamp_raw_versions([changed])
    legacy = _file_source(guide, source_id="project:legacy")  # restored pre-deploy row
    manifest = build_guidance_manifest(_snapshot([changed, legacy]), HEADER)
    reasons = {entry["source_id"]: entry["version_reason"] for entry in manifest.entries}
    assert reasons == {
        changed.source_id: "changed_since_read",
        "project:legacy": "retained_before_manifest",
    }
    assert all(entry["git_blob_oid"] is None for entry in manifest.entries)


def test_crlf_files_keep_their_version(tmp_path):
    """Loaders read with universal newlines; an unchanged CRLF file is not 'changed'."""
    path = tmp_path / "CRLF.md"
    path.write_bytes(b"line one\r\nline two\r\n")
    source = _file_source(path)
    stamp_raw_versions([source])
    assert source.raw_version["status"] == "read"
    assert source.raw_version["git_blob_oid"] == git_blob_oid(path.read_bytes())


def test_snapshot_is_found_on_every_harness_carrier():
    snapshot = _snapshot([_row("a", "project", "a")])
    frame = _snapshot([_row("b", "project", "b")])
    native = SimpleNamespace(snapshot=snapshot)
    claude = SimpleNamespace(
        seed_reconstruction=SimpleNamespace(snapshot=snapshot),
        frame_reconstruction=SimpleNamespace(snapshot=frame),
    )
    codex = SimpleNamespace(reconstruction=SimpleNamespace(snapshot=snapshot))
    wrapped = SimpleNamespace(rendered=SimpleNamespace(payload=codex))
    assert prepared_snapshot(native) is snapshot
    assert prepared_snapshot(claude) is frame  # the frame is what this Turn sends
    assert prepared_snapshot(wrapped) is snapshot
    assert prepared_snapshot(SimpleNamespace(rendered=SimpleNamespace(payload=None))) is None


def test_native_prepared_context_carries_its_snapshot_privately():
    from dataclasses import fields

    from core.turn_context import NativePreparedContext

    field = {item.name: item for item in fields(NativePreparedContext)}["snapshot"]
    assert field.compare is False and field.repr is False


# -- recording ------------------------------------------------------------------


class _KeepOpen:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def __getattr__(self, name: str):
        return getattr(self.db, name)

    def close(self) -> None:
        return None


@pytest.fixture
def db(monkeypatch) -> Generator[sqlite3.Connection, None, None]:
    # Tool reads run their gate off the event loop, on a worker thread.
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO threads (id,user_id,created_at,updated_at) "
        "VALUES ('thread-1','user-1','t0','t0')"
    )
    for turn, status in (("turn-1", "running"), ("turn-2", "completed")):
        connection.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) VALUES (?,'thread-1',0,'m',?,'t0','b')",
            (turn, status),
        )
    connection.commit()
    monkeypatch.setattr("infra.database.get_db", lambda: _KeepOpen(connection))
    yield connection
    connection.close()


def _turn(db, turn_id="turn-1"):
    return db.execute(
        "SELECT context_manifest_id, context_manifest_status FROM active_turns WHERE id=?",
        (turn_id,),
    ).fetchone()


def test_two_turns_with_the_same_guidance_share_one_row(db, guide):
    source = _file_source(guide)
    stamp_raw_versions([source])
    first = record_turn_manifest(
        "thread-1", "turn-1", SimpleNamespace(snapshot=_snapshot([source])), HEADER,
    )
    second = record_turn_manifest(
        "thread-1", "turn-2",
        SimpleNamespace(snapshot=_snapshot([source], history=[{"role": "user", "content": "x"}])),
        HEADER,
    )
    assert first == second and first.startswith("gm-")
    assert db.execute("SELECT COUNT(*) FROM context_manifests").fetchone()[0] == 1
    assert tuple(_turn(db, "turn-2")) == (first, "recorded")
    row = db.execute("SELECT * FROM context_manifests").fetchone()
    assert (row["harness"], row["selector"], row["resolved_model"]) == (
        "claude-code", "claude-code/sonnet", "claude-sonnet-5",
    )
    (entry,) = json.loads(row["entries_json"])
    assert "prompt_content" not in entry and "ALPHA" not in row["entries_json"]


def test_a_missing_snapshot_is_a_status_not_silence(db):
    assert record_turn_manifest("thread-1", "turn-1", SimpleNamespace(), HEADER) is None
    assert tuple(_turn(db)) == (None, "no_snapshot")


def test_a_build_failure_never_blocks_the_turn(db, monkeypatch):
    def broken(*_args):
        raise ValueError("boom")

    monkeypatch.setattr("core.guidance_manifest.build_guidance_manifest", broken)
    prepared = SimpleNamespace(snapshot=_snapshot([_row("a", "project", "a")]))
    assert record_turn_manifest("thread-1", "turn-1", prepared, HEADER) is None
    assert tuple(_turn(db)) == (None, "build_failed")


def test_a_write_failure_never_blocks_the_turn(db, monkeypatch):
    def broken(*_args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr("core.guidance_manifest._write_manifest", broken)
    prepared = SimpleNamespace(snapshot=_snapshot([_row("a", "project", "a")]))
    assert record_turn_manifest("thread-1", "turn-1", prepared, HEADER) is None
    assert tuple(_turn(db)) == (None, "write_failed")
    assert db.execute("SELECT COUNT(*) FROM context_manifests").fetchone()[0] == 0


def test_a_turn_whose_binding_never_landed_reads_unbound(db):
    """Distinct from NULL, which only rows older than the manifest columns carry."""
    assert tuple(_turn(db)) == (None, "unbound")


def test_an_unrecorded_skill_read_is_a_status_on_the_turn(db):
    db.execute("DROP TABLE guidance_loads")
    record_guidance_load(
        thread_id="thread-1", turn_id="turn-1", path=Path("skills/x/SKILL.md"),
        data=b"x", returned_text="x", partial=False,
    )
    assert db.execute(
        "SELECT guidance_load_status FROM active_turns WHERE id='turn-1'",
    ).fetchone()[0] == "write_failed"
    assert db.execute(
        "SELECT guidance_load_status FROM active_turns WHERE id='turn-2'",
    ).fetchone()[0] is None


def test_native_headers_name_the_transport_model():
    header = ManifestHeader.from_target(SimpleNamespace(
        harness="native", catalog_id="native/qwen", expected_reported_model=None,
        transport_model="openai/qwen3.8-flash",
    ))
    assert (header.resolved_model, header.resolved_model_source) == (
        "openai/qwen3.8-flash", "transport",
    )


# -- usage rows -----------------------------------------------------------------


def _store(db, key: str, turn_id: str = "turn-1"):
    from infra.turn_usage_receipts import (
        project_turn_usage_receipt,
        store_turn_usage_receipt,
    )

    identity, _projected, _binding = store_turn_usage_receipt(
        db, thread_id="thread-1", turn_id=turn_id, usage_key=key, model="m",
        measured={"prompt_tokens": 10, "completion_tokens": 2}, ttft_ms=None,
        source="conversation:native", now_iso="t1",
    )
    return identity, project_turn_usage_receipt


def test_a_mid_turn_checkpoint_moves_only_later_calls(db, guide):
    """Each call keeps the list its Turn held when the call was stored."""
    source = _file_source(guide)
    stamp_raw_versions([source])
    before = record_turn_manifest(
        "thread-1", "turn-1", SimpleNamespace(snapshot=_snapshot([source])), HEADER,
    )
    first, project = _store(db, "provider-call:1")
    project(db, "turn-1", first)
    checkpoint = _row("history:portable-checkpoint", "history", "narrative")
    after = record_turn_manifest(
        "thread-1", "turn-1", SimpleNamespace(snapshot=_snapshot([source, checkpoint])),
        HEADER,
    )
    second, project = _store(db, "provider-call:2")
    project(db, "turn-1", second)
    rows = dict(db.execute("SELECT id, context_manifest_id FROM llm_usage").fetchall())
    assert before != after
    assert rows == {first: before, second: after}


def test_a_restart_projects_the_list_the_receipt_was_stored_with(db):
    from infra.turn_usage_receipts import reconcile_pending_turn_usage

    listed = record_turn_manifest(
        "thread-1", "turn-1",
        SimpleNamespace(snapshot=_snapshot([_row("a", "project", "a")])), HEADER,
    )
    identity, _project = _store(db, "provider-call:1")  # killed before projection
    db.execute("UPDATE active_turns SET context_manifest_id='gm-later' WHERE id='turn-1'")
    db.commit()
    assert reconcile_pending_turn_usage(_KeepOpen(db)) == 1
    assert db.execute(
        "SELECT context_manifest_id FROM llm_usage WHERE id=?", (identity,),
    ).fetchone()[0] == listed


def test_rows_without_a_turn_carry_no_list(db):
    from infra.turn_usage_receipts import write_usage_row

    write_usage_row(
        db, usage_id="direct", thread_id="thread-1", turn_id=None, model="m",
        measured={}, ttft_ms=None,
        binding=dict.fromkeys(
            ("plan_id", "plan_node_id", "plan_attempt_id", "plan_commitment_id",
             "plan_usage_role"),
        ),
        source="checkpoint", now_iso="t1",
    )
    assert db.execute(
        "SELECT context_manifest_id FROM llm_usage WHERE id='direct'",
    ).fetchone()[0] is None


# -- mid-Turn skill reads -----------------------------------------------------------


@pytest.fixture
def skill(isolated_db, tmp_path, monkeypatch):
    from infra.database import get_db
    from tools import sandbox_config

    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RW", [tmp_path])
    monkeypatch.setattr(sandbox_config, "_STATIC_ALLOWED_RO", [])
    monkeypatch.setattr(sandbox_config, "_list_authoritative_repo_policies", lambda: [])
    with closing(get_db()) as db:
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('thread-1','user-1','t0','t0')"
        )
        db.commit()
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\n---\n" + "".join(f"line {n}\n" for n in range(20)))
    return path


def _loads():
    from infra.database import get_db

    with closing(get_db()) as db:
        return [
            tuple(row) for row in db.execute(
                "SELECT thread_id, turn_id, git_blob_oid, partial FROM guidance_loads"
                " ORDER BY partial"
            ).fetchall()
        ]


async def _read(path, **kwargs):
    from infra import state as runtime_state
    from tools.filesystem_rw import ReadFileTool

    token = runtime_state._current_turn_identity.set(SimpleNamespace(turn_id="turn-1"))
    try:
        return await ReadFileTool(workspace=path.parents[2]).execute(
            str(path), _thread_id="thread-1", **kwargs,
        )
    finally:
        runtime_state._current_turn_identity.reset(token)


@pytest.mark.asyncio
async def test_a_skill_read_leaves_a_version_receipt(skill):
    full = await _read(skill)
    assert "line 19" in full
    await _read(skill, offset=2, limit=3)
    oid = git_blob_oid(skill.read_bytes())
    assert _loads() == [("thread-1", "turn-1", oid, 0), ("thread-1", "turn-1", oid, 1)]


@pytest.mark.asyncio
async def test_failed_and_ordinary_reads_leave_nothing(skill):
    other = skill.parent / "NOTES.md"
    other.write_text("notes")
    assert (await _read(skill.parent / "missing" / "SKILL.md")).startswith("Error")
    assert "notes" in await _read(other)
    assert _loads() == []


def test_a_skill_receipt_hashes_the_bytes_the_read_returned(skill):
    data = skill.read_bytes()
    skill.write_text("swapped after the read")
    record_guidance_load(
        thread_id="thread-1", turn_id="turn-1", path=skill, data=data,
        returned_text="x", partial=False,
    )
    assert _loads() == [("thread-1", "turn-1", git_blob_oid(data), 0)]


@pytest.mark.asyncio
async def test_partial_comes_from_the_render_not_marker_text(skill, monkeypatch):
    from tools.filesystem_rw import ReadFileTool

    skill.write_text("... (truncated at 2000 lines. Total: 9 lines.)\n")
    await _read(skill)
    await _read(skill, offset=1, limit=5)  # a window that is the whole file
    assert [row[3] for row in _loads()] == [0, 0]
    monkeypatch.setattr(ReadFileTool, "OUTPUT_CHAR_CAP", 10)
    await _read(skill)
    assert [row[3] for row in _loads()] == [0, 0, 1]


# -- schema ---------------------------------------------------------------------


def test_migration_adds_the_tables_and_columns_once(tmp_path):
    from infra.database import initialize_schema
    from infra.database_migrations import migrate_to_current
    from infra.schema import SCHEMA_VERSION

    def shape(db):
        return {
            table: [row[1] for row in db.execute(f"PRAGMA table_info({table})")]
            for table in ("context_manifests", "guidance_loads", "llm_usage", "active_turns")
        }

    with closing(sqlite3.connect(":memory:")) as fresh:
        initialize_schema(fresh)
        expected = shape(fresh)
    path = tmp_path / "legacy.db"
    with closing(sqlite3.connect(path)) as db:
        initialize_schema(db)
        db.execute("DROP TABLE context_manifests")
        db.execute("DROP TABLE guidance_loads")
        db.execute("DROP INDEX idx_llm_usage_context_manifest")
        db.execute("ALTER TABLE llm_usage DROP COLUMN reasoning_effort")
        db.execute("ALTER TABLE llm_usage DROP COLUMN context_manifest_id")
        db.execute("ALTER TABLE active_turns DROP COLUMN context_manifest_id")
        db.execute("ALTER TABLE active_turns DROP COLUMN context_manifest_status")
        db.execute("ALTER TABLE active_turns DROP COLUMN guidance_load_status")
        db.execute(
            "INSERT INTO threads (id,user_id,created_at,updated_at) "
            "VALUES ('thread-1','user-1','t0','t0')"
        )
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) VALUES ('old','thread-1',0,'m','completed','t0','b')"
        )
        rewind_to_v88_policy(db)
        db.execute('ALTER TABLE project_vendor_accounts DROP COLUMN switch_on_limit')
        db.execute("DROP TABLE system_bulletins")
        db.execute("DROP TABLE system_bulletin_identity")
        db.execute("DROP TABLE measure_files")
        db.execute("PRAGMA user_version=46")
        db.commit()
    with closing(sqlite3.connect(path)) as db:
        result = migrate_to_current(db, SCHEMA_VERSION)
        assert (result.migrated, result.from_version, result.to_version) == (
            True, 46, SCHEMA_VERSION,
        )
        assert shape(db) == expected
        assert not migrate_to_current(db, SCHEMA_VERSION).migrated
        db.execute(
            "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,"
            "started_at,boot_epoch) VALUES ('new','thread-1',0,'m','running','t1','b')"
        )
        statuses = dict(db.execute(
            "SELECT id, context_manifest_status FROM active_turns",
        ).fetchall())
        assert statuses == {"old": None, "new": "unbound"}
