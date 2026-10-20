from __future__ import annotations

import time
from types import SimpleNamespace

import yaml

from core.skill_sidecar import installed_digest
from registry import models
from tests.event_routing_helpers import mk_session


def _item(*, withdrawn: bool = False) -> dict:
    return {
        "name": "pdf",
        "description": "PDF work.",
        "version": "2.0.0",
        "license": "Apache-2.0",
        "source": {"repository": "openai/skills", "commit": "b" * 40, "path": "skills/pdf"},
        "files": [{"path": "SKILL.md", "sha256": "sha256:" + "a" * 64}],
        "withdrawn": withdrawn,
        "withdrawn_reason": "unsafe" if withdrawn else None,
    }


def _copy(folder, *, version="1.0.0"):
    folder.mkdir(parents=True)
    skill = b"---\nname: pdf\ndescription: PDF work\n---\nUse it.\n"
    origin = {
        "source": "openai/skills@" + "a" * 40 + ":skills/pdf",
        "sha256": "a" * 64,
        "version": version,
        "adopted_at": "2026-10-19T00:00:00+00:00",
    }
    files = {"SKILL.md": skill, "corsia.yaml": yaml.safe_dump({"origin": origin}).encode()}
    origin["installed_digest"] = installed_digest(files)
    (folder / "SKILL.md").write_bytes(skill)
    (folder / "corsia.yaml").write_text(yaml.safe_dump({"origin": origin}))


def _count(sql: str, *args) -> int:
    from infra.database import get_db

    db = get_db()
    try:
        return db.execute(sql, args).fetchone()[0]
    finally:
        db.close()


def test_runtime_lifecycle_tracks_the_daily_library_refresh_task():
    from app_startup_runtime import RUNTIME_LOOP_TASKS
    from infra import state

    assert ("library refresh", "_library_refresh_task") in RUNTIME_LOOP_TASKS
    assert hasattr(state, "_library_refresh_task")


def test_producer_handles_1000_copies_under_five_seconds_on_an_indexed_query(isolated_db, tmp_path, monkeypatch):
    """The daily job scales with library copies, never the routing-registration table."""
    from infra.database import get_db
    from library import producer

    owner = mk_session("library-scale-owner")
    items = []
    for number in range(1000):
        name = f"pdf-{number}"
        folder = tmp_path / "skills" / name
        _copy(folder)
        models.register_artifact(
            integration="library", type="installed-skill", external_id=f"{owner}/{name}",
            parent_thread_id=owner, path=str(folder / "SKILL.md"),
        )
        items.append({**_item(), "name": name})
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(skills=tuple(items), index_age="just now"))
    monkeypatch.setattr("events.platform.deliver_platform_event_sync", lambda *_args: [])

    started = time.perf_counter()
    assert producer.produce_once() == 0
    elapsed = time.perf_counter() - started

    db = get_db()
    try:
        plan = db.execute(
            "EXPLAIN QUERY PLAN " + producer.INSTALLED_ROWS_SQL,
            ("library", "installed-skill"),
        ).fetchall()
    finally:
        db.close()
    details = " ".join(row[3] for row in plan)
    assert elapsed < 5
    assert "USING INDEX" in details
    assert "SCAN artifact_registrations" not in details


def test_producer_reads_installed_artifacts_and_delivers_each_change_once(isolated_db, tmp_path, monkeypatch):
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(skills=(_item(),), index_age="just now"))

    first = producer.produce_once()
    repeat = producer.produce_once()

    assert (first, repeat) == (1, 0)
    assert _count("SELECT COUNT(*) FROM context_change_journal WHERE object_id=?", owner) == 1


def test_producer_notices_a_version_change_at_the_same_source(isolated_db, tmp_path):
    from library import producer
    from library.items import item_manifest, source_ref

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    current = _item()
    current["source"]["commit"] = "a" * 40
    prior = {**current, "version": "1.0.0"}
    sidecar = yaml.safe_load((skill / "corsia.yaml").read_text())
    sidecar["origin"]["source"] = source_ref(current)
    sidecar["origin"]["item_manifest"] = item_manifest(prior)
    (skill / "corsia.yaml").write_text(yaml.safe_dump(sidecar))
    row = {"external_id": f"{owner}/pdf", "path": str(skill / "SKILL.md")}

    payload = producer._notice_payload(row, {"pdf": current})

    assert payload is not None
    assert payload["event_type"] == "library.item_changed"
    assert payload["installed_version"] == "1.0.0"
    assert payload["library_version"] == "2.0.0"


def test_producer_withdrawal_wakes_the_registered_owner_once(isolated_db, tmp_path, monkeypatch):
    from library import producer

    owner = mk_session("library-owner")
    skill = tmp_path / "skills" / "pdf"
    _copy(skill)
    models.register_artifact(
        integration="library", type="installed-skill", external_id=f"{owner}/pdf",
        parent_thread_id=owner, path=str(skill / "SKILL.md"),
    )
    monkeypatch.setattr("library.load_index", lambda: SimpleNamespace(skills=(_item(withdrawn=True),), index_age="just now"))

    producer.produce_once()
    producer.produce_once()

    assert _count("SELECT COUNT(*) FROM messages WHERE thread_id=? AND id LIKE 'platform-event:%'", owner) == 1
