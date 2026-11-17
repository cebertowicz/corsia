
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

_BACKEND_DIR = os.path.dirname(os.path.dirname(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


class _NoClose:
    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def close(self):
        try:
            object.__getattribute__(self, "_conn").rollback()
        except sqlite3.Error:
            pass

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)


@pytest.fixture
def caps_db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "caps.db"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.commit()
    wrapped = _NoClose(conn)
    from infra import settings as settings_mod
    monkeypatch.setattr(settings_mod, "get_db", lambda: wrapped)
    monkeypatch.setattr("infra.database.get_db", lambda: wrapped)
    yield conn
    conn.close()


def test_seed_writes_fields_from_data_file(caps_db):
    from infra.settings import get_setting
    from infra.subagent_caps import seed_subagent_caps

    assert seed_subagent_caps() is True
    row = get_setting("subagents")
    assert row["idle_retire_after_seconds"] > 0 and row["max_idle_live"] >= 0
    assert "max_concurrent_live" not in row


def test_seed_is_idempotent_and_non_clobbering(caps_db):
    from infra.settings import get_setting, set_setting
    from infra.subagent_caps import seed_subagent_caps

    operator = {"idle_retire_after_seconds": 5, "max_idle_live": 1}
    set_setting("subagents", operator)
    assert seed_subagent_caps() is False                  # fields present → no-op
    assert get_setting("subagents") == operator


def test_seed_drops_the_retired_working_cap(caps_db):
    from infra.settings import get_setting, set_setting
    from infra.subagent_caps import seed_subagent_caps

    set_setting("subagents", {"max_concurrent_live": 8, "idle_retire_after_seconds": 5,
                              "max_idle_live": 1, "other": True})
    assert seed_subagent_caps() is True
    assert get_setting("subagents") == {"idle_retire_after_seconds": 5, "max_idle_live": 1, "other": True}
    assert seed_subagent_caps() is False


def test_no_hardcoded_literal_in_code(caps_db):
    import json
    from pathlib import Path

    import infra.subagent_caps as caps
    seed_path = Path(caps.__file__).parent / "subagent_caps.json"
    data = json.loads(seed_path.read_text())
    assert isinstance(data["idle_retire_after_seconds"], int)
    assert isinstance(data["max_idle_live"], int)
    assert "max_concurrent_live" not in data


def test_seed_fills_each_missing_idle_field_without_clobbering(caps_db):
    from infra.settings import get_setting, set_setting
    from infra.subagent_caps import _load_seed_value, seed_subagent_caps

    set_setting("subagents", {"max_idle_live": 0})
    assert seed_subagent_caps() is True
    row = get_setting("subagents")
    assert row["max_idle_live"] == 0
    assert row["idle_retire_after_seconds"] == _load_seed_value("idle_retire_after_seconds")
    assert seed_subagent_caps() is False


def test_idle_resolvers_prefer_settings_then_seed(caps_db):
    from infra.settings import set_setting
    from infra.subagent_caps import (
        _load_seed_value, get_idle_retire_after_seconds, get_max_idle_live,
    )

    assert get_idle_retire_after_seconds() == _load_seed_value("idle_retire_after_seconds")
    assert get_max_idle_live() == _load_seed_value("max_idle_live")
    set_setting("subagents", {"idle_retire_after_seconds": 30, "max_idle_live": 1})
    assert get_idle_retire_after_seconds() == 30 and get_max_idle_live() == 1


def test_idle_fields_are_data(caps_db):
    import json
    from pathlib import Path

    import infra.subagent_caps as caps
    data = json.loads((Path(caps.__file__).parent / "subagent_caps.json").read_text())
    assert isinstance(data["idle_retire_after_seconds"], int)
    assert data["idle_retire_after_seconds"] > 0
    assert isinstance(data["max_idle_live"], int) and data["max_idle_live"] >= 0
