from __future__ import annotations

import json
import os

import pytest
from loguru import logger

from infra.database import get_db
from infra.moved_state_paths import heal_moved_state_paths


@pytest.fixture
def roots(tmp_path):
    legacy = tmp_path / "support" / "com.cebertowicz.corsia" / "state"
    current = tmp_path / "support" / "CORSIA" / "state"
    legacy.mkdir(parents=True)
    current.mkdir(parents=True)
    return legacy, current


def _connection_id(provider: str) -> str:
    return get_db().execute("SELECT id FROM connections WHERE provider=?", (provider,)).fetchone()["id"]


def _insert(account_id, provider, source, runtime, discovery=None):
    db = get_db()
    db.execute(
        "INSERT INTO vendor_accounts (id,connection_id,name,source_home,runtime_home,state,discovery_json,"
        "created_at,updated_at) VALUES (?,?,?,?,?,'signed_in',?,'t','t')",
        (account_id, _connection_id(provider), account_id, source, runtime,
         None if discovery is None else json.dumps(discovery)),
    )
    db.commit()


def _homes(account_id):
    row = get_db().execute("SELECT source_home, runtime_home, discovery_json FROM vendor_accounts WHERE id=?",
                           (account_id,)).fetchone()
    return row["source_home"], row["runtime_home"], row["discovery_json"]


def _make_homes(root, account_id):
    base = root / "runtime" / "vendor-accounts" / account_id
    (base / "source").mkdir(parents=True)
    (base / "runtime").mkdir(parents=True)
    return base / "source", base / "runtime"


def test_claude_custom_account_is_repointed(isolated_db, roots):
    legacy, current = roots
    old_source, old_runtime = (legacy / "runtime/vendor-accounts/c1/source",
                               legacy / "runtime/vendor-accounts/c1/runtime")
    new_source, new_runtime = _make_homes(current, "c1")
    _insert("c1", "claude-code", str(old_source), str(old_runtime))

    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 2

    assert _homes("c1")[:2] == (str(new_source), str(new_runtime))
    assert not (legacy / "runtime").exists()


def test_codex_custom_account_is_repointed_with_its_projection_and_link(isolated_db, roots):
    legacy, current = roots
    new_source, new_runtime = _make_homes(current, "x1")
    (new_source / "auth.json").write_text("{}")
    old_source = legacy / "runtime/vendor-accounts/x1/source"
    old_runtime = legacy / "runtime/vendor-accounts/x1/runtime"
    # The managed link was made before the move and still names the old source file.
    (new_runtime / "auth.json").symlink_to(old_source / "auth.json")
    entry = {"source": str(old_source / "auth.json"), "destination": str(old_runtime / "auth.json"),
             "home": str(old_runtime), "kind": "live", "phase": "established"}
    discovery = {"auth_projection": {"failed": None, "stale": [], "homes": {entry["destination"]: entry}}}
    _insert("x1", "codex", str(old_source), str(old_runtime), discovery)

    heal_moved_state_paths(legacy_root=legacy, state_root=current)

    source, runtime, stored = _homes("x1")
    assert (source, runtime) == (str(new_source), str(new_runtime))
    healed = json.loads(stored)["auth_projection"]["homes"]
    assert list(healed) == [str(new_runtime / "auth.json")]
    assert healed[str(new_runtime / "auth.json")]["source"] == str(new_source / "auth.json")
    assert (new_runtime / "auth.json").is_symlink()
    assert str((new_runtime / "auth.json").readlink()) == str(new_source / "auth.json")


def test_missing_target_is_left_alone_and_logged(isolated_db, roots):
    legacy, current = roots
    old = legacy / "runtime/vendor-accounts/m1"
    _insert("m1", "claude-code", str(old / "source"), str(old / "runtime"))
    messages: list[str] = []
    sink = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 0
    finally:
        logger.remove(sink)

    assert _homes("m1")[:2] == (str(old / "source"), str(old / "runtime"))
    assert any("source_home" in message for message in messages)


def test_second_run_changes_nothing(isolated_db, roots):
    legacy, current = roots
    new_source, new_runtime = _make_homes(current, "i1")
    _insert("i1", "claude-code", str(legacy / "runtime/vendor-accounts/i1/source"),
            str(legacy / "runtime/vendor-accounts/i1/runtime"))
    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 2
    before = get_db().total_changes

    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 0
    assert get_db().total_changes == before
    assert _homes("i1")[:2] == (str(new_source), str(new_runtime))


def test_rows_outside_the_legacy_root_are_untouched(isolated_db, roots, tmp_path):
    legacy, current = roots
    own = tmp_path / "elsewhere" / "claude"
    sibling = legacy.parent / "state-other" / "source"  # shares the legacy prefix as text only
    for home in (own, sibling):
        home.mkdir(parents=True)
    _insert("o1", "claude-code", str(own), str(own.parent / "runtime"))
    _insert("o2", "claude-code", str(sibling), str(sibling.parent / "runtime"))
    _make_homes(current, "o1")

    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 0

    assert _homes("o1")[:2] == (str(own), str(own.parent / "runtime"))
    assert _homes("o2")[:2] == (str(sibling), str(sibling.parent / "runtime"))


def test_emptied_old_homes_are_removed_but_never_a_file_or_the_root(isolated_db, roots):
    legacy, current = roots
    for account in ("e1", "f1"):
        _make_homes(current, account)
        for leaf in ("source", "runtime"):
            (legacy / "runtime/vendor-accounts" / account / leaf).mkdir(parents=True)
        _insert(account, "claude-code", str(legacy / f"runtime/vendor-accounts/{account}/source"),
                str(legacy / f"runtime/vendor-accounts/{account}/runtime"))
    (legacy / "runtime/vendor-accounts/f1/runtime/keep.txt").write_text("x")

    heal_moved_state_paths(legacy_root=legacy, state_root=current)

    assert not (legacy / "runtime/vendor-accounts/e1").exists()
    assert (legacy / "runtime/vendor-accounts/f1/runtime/keep.txt").read_text() == "x"
    assert legacy.is_dir()


def test_an_empty_recreated_tree_is_gone_after_the_heal(isolated_db, roots):
    legacy, current = roots
    _make_homes(current, "g1")
    for leaf in ("source", "runtime"):
        (legacy / "runtime/vendor-accounts/g1" / leaf).mkdir(parents=True)
    _insert("g1", "claude-code", str(legacy / "runtime/vendor-accounts/g1/source"),
            str(legacy / "runtime/vendor-accounts/g1/runtime"))

    heal_moved_state_paths(legacy_root=legacy, state_root=current)

    assert not (legacy / "runtime").exists()
    assert legacy.is_dir()


def test_failed_link_repoint_leaves_the_row_as_stored_and_the_next_start_heals_it(isolated_db, roots, monkeypatch):
    legacy, current = roots
    new_source, new_runtime = _make_homes(current, "x2")
    (new_source / "auth.json").write_text("{}")
    old_source = legacy / "runtime/vendor-accounts/x2/source"
    old_runtime = legacy / "runtime/vendor-accounts/x2/runtime"
    (new_runtime / "auth.json").symlink_to(old_source / "auth.json")
    entry = {"source": str(old_source / "auth.json"), "destination": str(old_runtime / "auth.json"),
             "home": str(old_runtime), "kind": "live", "phase": "established"}
    discovery = {"auth_projection": {"failed": None, "stale": [], "homes": {entry["destination"]: entry}}}
    _insert("x2", "codex", str(old_source), str(old_runtime), discovery)
    stored_before = _homes("x2")

    real_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(PermissionError("denied")))
    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) == 0
    assert _homes("x2") == stored_before
    assert not [p for p in new_runtime.iterdir() if p.name.startswith(".corsia-auth-")]

    monkeypatch.setattr(os, "replace", real_replace)
    assert heal_moved_state_paths(legacy_root=legacy, state_root=current) > 0
    assert _homes("x2")[:2] == (str(new_source), str(new_runtime))
    assert os.readlink(new_runtime / "auth.json") == str(new_source / "auth.json")
