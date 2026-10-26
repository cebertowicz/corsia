"""A vendor account's stored homes follow the current state root, so a move never recreates the old one."""
from __future__ import annotations

import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from connections import account_profiles
from connections.account_row_shape import current_home, project_row


def _row(source, runtime):
    return {"id": "abc", "provider": "codex", "source_home": source, "runtime_home": runtime,
            "identity_json": "{}", "auth_cleanup_json": None, "discovery_json": None}


def test_homes_stored_under_the_legacy_root_are_read_on_the_current_root(tmp_path, monkeypatch):
    current = tmp_path / "CORSIA" / "state" / "runtime" / "vendor-accounts"
    monkeypatch.setitem(account_profiles._PROFILES, "codex",
                        replace(account_profiles._PROFILES["codex"], home_root=lambda: current))
    (current / "abc" / "source").mkdir(parents=True)
    (current / "abc" / "runtime").mkdir()
    legacy = tmp_path / "com.cebertowicz.corsia" / "state" / "runtime" / "vendor-accounts" / "abc"
    account = project_row(_row(str(legacy / "source"), str(legacy / "runtime")))
    assert account["source_home"] == str(current / "abc" / "source")
    assert account["runtime_home"] == str(current / "abc" / "runtime")


def test_signed_out_and_unrecognised_homes_are_kept():
    assert current_home(None, "codex", "abc") is None
    assert current_home("/somewhere/else", "codex", "abc") == "/somewhere/else"


def test_a_home_that_still_exists_is_kept_verbatim(tmp_path):
    here = tmp_path / "abc" / "source"
    here.mkdir(parents=True)
    assert current_home(str(here), "codex", "abc") == str(here)
