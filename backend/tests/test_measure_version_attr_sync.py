"""The file metadata and its definitions are committed together."""
import sqlite3

import pytest

from infra.measure_registry import REGISTRY, WorkflowOwner, sync, workflow_for_file
from infra.outcome_events_schema import OUTCOME_EVENTS_SCHEMA_SQL

TEXT = """schema: corsia.measures/1
namespace: demo
workflow: example
version_attr: revision
events:
  demo.run:
    attributes: {revision: id}
    dedupe: [revision]
"""


def fixture(tmp_path):
    db = sqlite3.connect(":memory:")
    db.executescript(OUTCOME_EVENTS_SCHEMA_SQL)
    path = str(tmp_path / "measures.yaml")
    owners = {"example": WorkflowOwner(tmp_path, "owner")}
    return db, path, owners


def test_sync_stores_file_metadata_without_bumping_definition_versions(tmp_path):
    db, path, owners = fixture(tmp_path)
    assert db.execute("SELECT version_attr FROM measure_definitions WHERE file_path=?", (path,)).fetchone() is None
    sync(db, owner_thread_id="owner", path=path, text=TEXT, workflows=owners)
    assert db.execute("SELECT version_attr FROM measure_definitions").fetchone() == ("revision",)
    sync(db, owner_thread_id="owner", path=path, text=TEXT.replace("version_attr: revision\n", ""), workflows=owners)
    assert db.execute("SELECT version_attr FROM measure_definitions").fetchone() == (None,)
    assert db.execute("SELECT max(version) FROM measure_definitions").fetchone() == (1,)
    assert workflow_for_file(path, "owner", owners) == "example"
    assert workflow_for_file("/ws/.threads/owner/measures.yaml", "owner", owners) is None
    REGISTRY.rebuild(db)


def test_metadata_failure_rolls_back_definitions(tmp_path):
    db, path, owners = fixture(tmp_path)
    db.execute("CREATE TRIGGER fail_metadata BEFORE UPDATE OF version_attr ON measure_definitions BEGIN SELECT RAISE(ABORT, 'injected'); END")
    db.commit()
    with pytest.raises(ValueError, match="another sync"):
        sync(db, owner_thread_id="owner", path=path, text=TEXT, workflows=owners)
    assert db.execute("SELECT count(*) FROM measure_definitions").fetchone() == (0,)
