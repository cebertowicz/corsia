"""Keep the generated data-model reference in sync with the store schemas."""

from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/docs"))

import generate_data_model as generator  # noqa: E402
from docs_commands import regenerate_command  # noqa: E402


def test_data_model_reference_is_current():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/docs/generate_data_model.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"Data-model reference is out of date. Regenerate with:\n"
        f"{regenerate_command('generate_data_model')}\n{result.stdout}{result.stderr}"
    )


def test_unmapped_schema_module_fails_generation(tmp_path, monkeypatch):
    infra = tmp_path / "backend/infra"
    infra.mkdir(parents=True)
    for name in ("schema.py", "continuity_staging.py", "db_fts.py"):
        (infra / name).write_text("")
    (infra / "new_schema.py").write_text("CREATE TABLE new_table (id INTEGER);")
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="new_schema.py"):
        generator.source_map()


def test_regeneration_removes_obsolete_subsystem(tmp_path, monkeypatch):
    monkeypatch.setattr(generator, "OUTPUT", tmp_path)
    (tmp_path / "obsolete.md").write_text("old")
    assert generator.write_or_check({"README.md": "current"}, check=True)
    assert not generator.write_or_check({"README.md": "current"}, check=False)
    assert not (tmp_path / "obsolete.md").exists()
    assert (tmp_path / "README.md").read_text() == "current"


def test_partial_unique_index_is_one_to_many():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript("""
            CREATE TABLE parent (id INTEGER PRIMARY KEY);
            CREATE TABLE child (parent_id INTEGER REFERENCES parent(id), active INTEGER);
            CREATE UNIQUE INDEX child_active_parent ON child(parent_id) WHERE active = 1;
        """)
        tables = generator.sqlite_tables(connection)
    assert ("child_active_parent", "partial unique", "parent_id") in tables["child"]["indexes"]
    assert "parent o|--o{ child : parent_id" in generator.diagram(tables)
