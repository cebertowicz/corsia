"""The Codex warm source is resolved under the account its transcript ran on.

The rollout root comes from the frozen login of the thread's latest Codex Turn
through the canonical account homes, never from the current binding and never
by falling back to another account's or the Default's tree.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import core.fork_codex as fork
from infra.vendor_login import stamp_initial_claim
from vendor_account_fixtures import accounts  # noqa: F401

PROVIDER_ID = "11111111-2222-4333-8444-555555555555"
_NOW = datetime.now(timezone.utc)
ROLLOUT = f"rollout-{_NOW:%Y-%m-%dT%H-%M-%S}-{PROVIDER_ID}.jsonl"


def _rollout(root: Path, *, provider_id: str = PROVIDER_ID) -> Path:
    day = root / f"{_NOW:%Y}" / f"{_NOW:%m}" / f"{_NOW:%d}"
    day.mkdir(parents=True, exist_ok=True)
    path = day / ROLLOUT
    path.write_text(json.dumps({
        "type": "session_meta", "payload": {"id": provider_id, "cwd": "/work"},
    }) + "\n")
    return path


def _turn(db, account_id: str, *, stamp: bool = True, turn_id: str = "turn-1") -> None:
    db.execute(
        "INSERT INTO active_turns(id,thread_id,execution_epoch,model,status,started_at,"
        "boot_epoch,vendor_account_id) VALUES (?,?,0,'codex/test','completed','t','b',?)",
        (turn_id, "thread-a", account_id),
    )
    db.execute(
        "INSERT INTO messages (id,thread_id,role,content,created_at,seq,active_turn_id) "
        "VALUES (?,'thread-a','user','input','t',0,?)", (f"{turn_id}-in", turn_id),
    )
    if stamp:
        assert stamp_initial_claim(db, turn_id, f"{turn_id}-in") is not None
    db.commit()


@pytest.fixture
def roots(accounts, monkeypatch, tmp_path):  # noqa: F811
    db, legacy, custom, other = accounts
    default_home = tmp_path / "default-home"
    monkeypatch.setattr("subagents.codex_env._default_app_server_home", lambda: default_home)
    monkeypatch.setattr(
        "subagents.codex_runtime_support.codex_thread_registration",
        lambda _thread, _db: (PROVIDER_ID, registered["path"], "low"),
    )
    registered = {"path": None}
    return db, legacy, custom, other, default_home / "sessions", registered


def test_custom_account_resolves_its_own_runtime_root(roots) -> None:
    db, _legacy, custom, _other, _default_root, registered = roots
    _turn(db, custom["id"])
    expected = _rollout(Path(custom["runtime_home"]) / "sessions")
    registered["path"] = str(expected)

    result = fork._registered_rollout("thread-a")

    assert result.rollout == expected
    assert result.receipt.account_id == custom["id"]
    assert result[:3] == (PROVIDER_ID, expected, "low")


def test_custom_account_found_by_walk_without_recorded_path(roots) -> None:
    db, _legacy, custom, _other, _default_root, _registered = roots
    _turn(db, custom["id"])
    expected = _rollout(Path(custom["runtime_home"]) / "sessions")

    assert fork._registered_rollout("thread-a").rollout == expected


def test_custom_account_never_escapes_to_default_tree(roots) -> None:
    db, _legacy, custom, _other, default_root, registered = roots
    _turn(db, custom["id"])
    registered["path"] = str(_rollout(default_root))

    assert fork._registered_rollout("thread-a") is None


def test_custom_account_never_reads_same_provider_other_account(roots) -> None:
    db, _legacy, custom, other, _default_root, registered = roots
    _turn(db, custom["id"])
    registered["path"] = str(_rollout(Path(other["runtime_home"]) / "sessions"))

    assert fork._registered_rollout("thread-a") is None


def test_current_binding_does_not_move_the_root(roots) -> None:
    db, _legacy, custom, other, _default_root, registered = roots
    _turn(db, custom["id"])
    expected = _rollout(Path(custom["runtime_home"]) / "sessions")
    registered["path"] = str(expected)
    # The current binding moves to another account after the Turn ran.
    db.execute(
        "INSERT INTO thread_vendor_accounts(thread_id,provider,account_id) "
        "VALUES ('thread-a','codex',?)",
        (other["id"],),
    )
    db.commit()

    assert fork._registered_rollout("thread-a").rollout == expected


def test_no_turn_keeps_the_default_root_and_an_unstamped_account_turn_refuses(roots) -> None:
    db, legacy, _custom, _other, default_root, registered = roots
    expected = _rollout(default_root)
    registered["path"] = str(expected)

    assert fork._registered_rollout("thread-a").rollout == expected  # no Turn at all
    _turn(db, legacy["id"], stamp=False)
    # The Default is an account with its own home like any other: no receipt, no root.
    assert fork._registered_rollout("thread-a") == "account_provenance_ambiguous"


def test_custom_account_without_a_receipt_refuses(roots) -> None:
    db, _legacy, custom, _other, default_root, registered = roots
    _turn(db, custom["id"], stamp=False)
    registered["path"] = str(_rollout(default_root))

    assert fork._registered_rollout("thread-a") == "account_provenance_ambiguous"


def test_stale_receipt_refuses(roots) -> None:
    from connections.vendor_accounts import set_account_state

    db, _legacy, custom, _other, _default_root, registered = roots
    _turn(db, custom["id"])
    registered["path"] = str(_rollout(Path(custom["runtime_home"]) / "sessions"))
    current = db.execute(
        "SELECT revision FROM vendor_accounts WHERE id=?", (custom["id"],),
    ).fetchone()
    set_account_state(custom["id"], "signed_out", expected_revision=current["revision"], db=db)

    assert fork._registered_rollout("thread-a") == "account_provenance_ambiguous"


def test_warm_source_reuses_the_resolved_receipt_and_checks_the_header(roots) -> None:
    db, _legacy, custom, _other, _default_root, registered = roots
    _turn(db, custom["id"])
    path = _rollout(Path(custom["runtime_home"]) / "sessions")
    registered["path"] = str(path)

    proof = fork._warm_source_proof("thread-a", None)
    assert proof[3] == Path(custom["runtime_home"]) / "sessions"
    assert proof[-1].account_id == custom["id"]

    _rollout(Path(custom["runtime_home"]) / "sessions", provider_id="99999999-2222-4333-8444-555555555555")
    assert fork._warm_source_proof("thread-a", None) == "rollout_identity_mismatch"


def test_warm_source_surfaces_the_ambiguity_reason(roots) -> None:
    db, _legacy, custom, _other, _default_root, _registered = roots
    _turn(db, custom["id"], stamp=False)

    assert fork._warm_source_proof("thread-a", None) == "account_provenance_ambiguous"
    assert fork.warm_tool_boundary_prefix_bytes("thread-a", object()) == "account_provenance_ambiguous"


def test_real_registry_metadata_and_frozen_receipt_reach_the_warm_proof(roots_real) -> None:
    """No stub on codex_thread_registration: the registry row is the source."""
    from registry.service import register_codex_thread

    db, custom, default_root = roots_real
    db.execute(
        "INSERT OR IGNORE INTO threads(id,user_id,model,created_at,updated_at) "
        "VALUES ('thread-a','user','codex/test','t','t')"
    )
    _turn(db, custom["id"])
    path = _rollout(Path(custom["runtime_home"]) / "sessions")
    register_codex_thread(
        "thread-a", PROVIDER_ID, rollout_path=str(path), reasoning_effort="medium", db=db,
    )
    db.commit()

    registered = fork._registered_rollout("thread-a")
    assert registered.rollout == path
    assert registered.provider_id == PROVIDER_ID and registered.effort == "medium"
    assert registered.receipt.account_id == custom["id"]

    proof = fork._warm_source_proof("thread-a", None)
    assert proof[:4] == (PROVIDER_ID, path, "medium", Path(custom["runtime_home"]) / "sessions")
    assert proof[-1].account_id == custom["id"]
    assert not default_root.exists()  # the legacy root was never consulted


@pytest.fixture
def roots_real(accounts, monkeypatch, tmp_path):  # noqa: F811
    db, _legacy, custom, _other = accounts
    default_home = tmp_path / "default-home"
    monkeypatch.setattr("subagents.codex_env._default_app_server_home", lambda: default_home)
    return db, custom, default_home / "sessions"
