from __future__ import annotations

import json

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


def test_codex_custom_account_homes_are_repointed_and_its_projection_is_left_to_its_owner(isolated_db, roots):
    legacy, current = roots
    new_source, new_runtime = _make_homes(current, "x1")
    (new_source / "auth.json").write_text("{}")
    old_source = legacy / "runtime/vendor-accounts/x1/source"
    old_runtime = legacy / "runtime/vendor-accounts/x1/runtime"
    (new_runtime / "auth.json").symlink_to(old_source / "auth.json")
    entry = {"source": str(old_source / "auth.json"), "destination": str(old_runtime / "auth.json"),
             "home": str(old_runtime), "kind": "live", "phase": "established"}
    failed = {"cause": "codex_auth_source_missing", "account_revision": 1, "login_seq": 1}
    discovery = {"auth_projection": {"failed": failed, "stale": [], "homes": {entry["destination"]: entry}}}
    _insert("x1", "codex", str(old_source), str(old_runtime), discovery)
    _, _, stored_before = _homes("x1")

    heal_moved_state_paths(legacy_root=legacy, state_root=current)

    source, runtime, stored = _homes("x1")
    assert (source, runtime) == (str(new_source), str(new_runtime))
    assert stored == stored_before
    assert str((new_runtime / "auth.json").readlink()) == str(old_source / "auth.json")


@pytest.mark.parametrize("unexpected_link", [False, True])
def test_moved_codex_projection_recovers_without_a_new_login(isolated_db, roots, unexpected_link):
    from connections.account_evidence import readiness_reason
    from types import SimpleNamespace
    from connections.account_projection import projection_gate
    from connections.account_policy import AccountAdmissionError, select_account
    from subagents.vendor_write_admission import prepare_session_launch
    from tests.test_checkpoint_store import _seed_thread
    from connections.account_receipts import ReceiptFence
    from connections.vendor_accounts import get_account

    legacy, current = roots
    new_source, new_runtime = _make_homes(current, "x2")
    (new_source / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"fixture": "synthetic"}}))
    old_source = legacy / "runtime/vendor-accounts/x2/source"
    old_runtime = legacy / "runtime/vendor-accounts/x2/runtime"
    old_auth, old_dest = str(old_source / "auth.json"), str(old_runtime / "auth.json")
    (new_runtime / "auth.json").symlink_to(old_auth)
    entry = {"source": old_auth, "destination": old_dest, "home": str(old_runtime),
             "kind": "live", "phase": "established", "account_revision": 0, "login_seq": 1,
             "owner_turn_id": None, "owner_boot_epoch": None, "at": "t"}
    failed = {"cause": "codex_auth_source_missing", "source": old_auth, "destination": old_dest,
              "account_revision": 0, "login_seq": 1, "at": "t"}
    record = {"auth_state": "known", "login_seq": 1, "login_revision": 0,
              "discovery": {"status": "ready", "receipt": {"account_revision": 0, "login_seq": 1},
                            "cache_receipt": None, "models": {"codex/test": {}}},
              "auth_projection": {"failed": failed, "stale": [], "homes": {old_dest: entry}}}
    _insert("x2", "codex", str(old_source), str(old_runtime), record)
    db = get_db()
    receipt = ReceiptFence("x2", 0, 1)
    _seed_thread(db, "moved-codex", model="codex/test")
    db.commit()
    select_account("moved-codex", "codex", account_id="x2", db=db)
    session = SimpleNamespace(corsia_thread_id="moved-codex", codex_home=None, source_codex_home=None,
                              catalog_model_id="codex/test")
    with pytest.raises(AccountAdmissionError, match="account_home_mismatch"):
        projection_gate(receipt, db=db)
    heal_moved_state_paths(legacy_root=legacy, state_root=current)
    if unexpected_link:
        (new_runtime / "auth.json").unlink()
        (new_runtime / "auth.json").symlink_to(new_source / "unexpected.json")
    from connections.account_projection import repair_moved_projections
    repair_moved_projections(legacy_root=legacy, state_root=current, db=db)
    if unexpected_link:
        assert readiness_reason(get_account("x2", db=db), model="codex/test") == "codex_auth_projection_changed"
        with pytest.raises(AccountAdmissionError, match="codex_auth_projection_changed"):
            prepare_session_launch(session, "codex")
        assert (new_runtime / "auth.json").readlink() == new_source / "unexpected.json"
        return

    assert get_account("x2", db=db)["discovery"]["auth_projection"]["failed"] is None
    assert not (new_runtime / "auth.json").is_symlink()
    assert readiness_reason(get_account("x2", db=db), model="codex/test") is None
    prepare_session_launch(session, "codex")

    account = get_account("x2", db=db)
    assert readiness_reason(account, model="codex/test") is None
    projection = account["discovery"]["auth_projection"]
    assert projection["failed"] is None
    assert old_dest not in projection["homes"]
    assert projection["homes"][str(new_runtime / "auth.json")]["source"] == str(new_source / "auth.json")
    assert (new_runtime / "auth.json").readlink() == new_source / "auth.json"


def test_restored_codex_auth_clears_legacy_missing_failure(isolated_db, roots):
    from types import SimpleNamespace
    from connections.account_evidence import readiness_reason
    from connections.account_policy import select_account
    from connections.vendor_accounts import get_account
    from subagents.vendor_write_admission import prepare_session_launch
    from tests.test_checkpoint_store import _seed_thread

    legacy, current = roots
    source, runtime = _make_homes(current, "x3")
    auth = source / "auth.json"
    failure = {"cause": "codex_auth_source_missing", "source": str(auth),
               "destination": str(runtime / "auth.json"), "account_revision": 0, "login_seq": 1, "at": "t"}
    record = {"auth_state": "known", "login_seq": 1, "login_revision": 0,
              "discovery": {"status": "ready", "receipt": {"account_revision": 0, "login_seq": 1},
                            "cache_receipt": None, "models": {"codex/test": {}}},
              "auth_projection": {"failed": failure, "stale": [], "homes": {}}}
    _insert("x3", "codex", str(source), str(runtime), record)
    db = get_db()
    _seed_thread(db, "restored-codex", model="codex/test")
    db.commit()
    select_account("restored-codex", "codex", account_id="x3", db=db)
    from connections.account_projection import repair_moved_projections
    repair_moved_projections(legacy_root=legacy, state_root=current, db=db)
    assert get_account("x3", db=db)["discovery"]["auth_projection"]["failed"] is None
    assert readiness_reason(get_account("x3", db=db), model="codex/test") == "codex_auth_source_missing"
    auth.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"fixture": "synthetic"}}))
    assert readiness_reason(get_account("x3", db=db), model="codex/test") is None
    session = SimpleNamespace(corsia_thread_id="restored-codex", codex_home=None, source_codex_home=None,
                              catalog_model_id="codex/test")
    prepare_session_launch(session, "codex")
    assert get_account("x3", db=db)["discovery"]["auth_projection"]["failed"] is None
    assert (runtime / "auth.json").readlink() == auth


def test_startup_repair_rewrites_failure_and_drops_absent_moved_home(isolated_db, roots):
    from connections.account_projection import repair_moved_projections
    from connections.vendor_accounts import get_account

    legacy, current = roots
    source, runtime = _make_homes(current, "x4")
    old_source = legacy / "runtime/vendor-accounts/x4/source/auth.json"
    old_dest = legacy / "runtime/vendor-accounts/x4/runtime/auth.json"
    failure = {"cause": "codex_auth_projection_changed", "source": str(old_source),
               "destination": str(old_dest), "account_revision": 0, "login_seq": 1, "at": "t"}
    entry = {"source": str(old_source), "destination": str(old_dest), "home": str(old_dest.parent),
             "kind": "live", "phase": "established", "account_revision": 0, "login_seq": 1}
    record = {"auth_state": "known", "login_seq": 1, "login_revision": 0,
              "auth_projection": {"failed": failure, "stale": [], "homes": {str(old_dest): entry}}}
    _insert("x4", "codex", str(source), str(runtime), record)

    repair_moved_projections(legacy_root=legacy, state_root=current)
    projection = get_account("x4")["discovery"]["auth_projection"]
    assert projection["homes"] == {}
    assert projection["failed"] == failure | {"source": str(source / "auth.json"),
                                                 "destination": str(runtime / "auth.json")}
    before = get_db().total_changes
    repair_moved_projections(legacy_root=legacy, state_root=current)
    assert get_db().total_changes == before


def test_startup_repair_unlinks_a_moved_temporary_home(isolated_db, roots):
    # A later retirement compares the link against the new source; left in place it would refuse the account.
    from connections.account_projection import repair_moved_projections
    from connections.vendor_accounts import get_account

    legacy, current = roots
    source, runtime = _make_homes(current, "x5")
    checkpoint = runtime.parent / "checkpoint-1"
    checkpoint.mkdir()
    old_source = legacy / "runtime/vendor-accounts/x5/source/auth.json"
    old_dest = legacy / "runtime/vendor-accounts/x5/checkpoint-1/auth.json"
    (checkpoint / "auth.json").symlink_to(old_source)
    entry = {"source": str(old_source), "destination": str(old_dest), "home": str(old_dest.parent),
             "kind": "checkpoint", "phase": "established", "account_revision": 0, "login_seq": 1,
             "owner_turn_id": "turn-1"}
    record = {"auth_state": "known", "login_seq": 1, "login_revision": 0,
              "auth_projection": {"failed": None, "stale": [], "homes": {str(old_dest): entry}}}
    _insert("x5", "codex", str(source), str(runtime), record)

    repair_moved_projections(legacy_root=legacy, state_root=current)
    projection = get_account("x5")["discovery"]["auth_projection"]
    assert projection["homes"] == {}
    assert projection["failed"] is None
    assert not (checkpoint / "auth.json").is_symlink()


def test_startup_repair_keeps_an_account_relinked_after_the_home_repair(isolated_db, roots):
    # A healthy account launched after heal_moved_state_paths already links its moved home to the new source.
    from connections.account_evidence import readiness_reason
    from connections.account_projection import repair_moved_projections
    from connections.vendor_accounts import get_account

    legacy, current = roots
    source, runtime = _make_homes(current, "x6")
    (source / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {"fixture": "synthetic"}}))
    (runtime / "auth.json").symlink_to(source / "auth.json")
    old_source = legacy / "runtime/vendor-accounts/x6/source/auth.json"
    old_dest = legacy / "runtime/vendor-accounts/x6/runtime/auth.json"
    stale = {"source": str(old_source), "destination": str(old_dest), "home": str(old_dest.parent),
             "kind": "live", "phase": "established", "account_revision": 0, "login_seq": 1}
    relinked = stale | {"source": str(source / "auth.json"), "destination": str(runtime / "auth.json"),
                        "home": str(runtime), "at": "later"}
    record = {"auth_state": "known", "login_seq": 1, "login_revision": 0,
              "discovery": {"status": "ready", "receipt": {"account_revision": 0, "login_seq": 1},
                            "cache_receipt": None, "models": {"codex/test": {}}},
              "auth_projection": {"failed": None, "stale": [],
                                  "homes": {str(old_dest): stale, str(runtime / "auth.json"): relinked}}}
    _insert("x6", "codex", str(source), str(runtime), record)

    repair_moved_projections(legacy_root=legacy, state_root=current)
    account = get_account("x6")
    projection = account["discovery"]["auth_projection"]
    assert projection["failed"] is None
    assert projection["homes"] == {str(runtime / "auth.json"): relinked}
    assert (runtime / "auth.json").readlink() == source / "auth.json"
    assert readiness_reason(account, model="codex/test") is None


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
