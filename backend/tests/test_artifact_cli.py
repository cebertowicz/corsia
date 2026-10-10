"""Item A — the ``corsia artifact`` CLI drives the audited lifecycle verbs.

Proves the CLI is a real path to release / rehome an artifact WITHOUT raw SQL —
the de-own pain that motivated PRD-ARTIFACTS-LIFECYCLE AR3a — by going through
the same Tool path the agent harnesses use.
"""

from __future__ import annotations

import json

import pytest

import cli as corsia_cli
from registry import artifact_record as AR
from tests.event_routing_helpers import mk_session


@pytest.fixture(autouse=True)
def _disable_analytics(monkeypatch):
    monkeypatch.setenv("CORSIA_ANALYTICS_DISABLE", "1")


IDENTITY = {"integration": "github", "type": "pull_request", "external_id": "702"}


def _run(argv: list[str], capsys) -> tuple[int, str]:
    rc = corsia_cli.main(argv)
    return rc, capsys.readouterr().out


def test_release_deowns_artifact(isolated_db, capsys):
    owner = mk_session("owner-thr")
    rec = AR.register_or_resolve(IDENTITY, owner_intent=owner)
    assert rec.owner_thread == owner

    rc, out = _run([
        "artifact", "release",
        "--as-session", owner,
        "--integration", "github", "--type", "pull_request", "--external-id", "702",
        "--reason", "wrong owner (CLI de-own)",
    ], capsys)

    assert rc == 0, out
    assert not out.startswith("Error"), out
    after = AR.register_or_resolve(IDENTITY)
    assert after.owner_thread != owner, f"expected de-owned, still owned by {owner}: {out}"


def test_rehome_transfers_owner(isolated_db, capsys):
    owner = mk_session("owner-thr")
    target = mk_session("new-owner-thr")
    AR.register_or_resolve(IDENTITY, owner_intent=owner)

    rc, out = _run([
        "artifact", "rehome",
        "--as-session", owner,
        "--integration", "github", "--type", "pull_request", "--external-id", "702",
        "--new-owner-thread-id", target,
        "--reason", "handoff (CLI)",
    ], capsys)

    assert rc == 0, out
    after = AR.register_or_resolve(IDENTITY)
    assert after.owner_thread == target, f"expected owner={target}, got {after.owner_thread}: {out}"


def test_resolve_reports_missing_without_error(isolated_db, capsys):
    owner = mk_session("owner-thr")
    rc, out = _run([
        "artifact", "resolve",
        "--as-session", owner,
        "--integration", "github", "--type", "pull_request", "--external-id", "does-not-exist",
    ], capsys)

    assert rc == 0, out
    assert json.loads(out) == {"found": False, "artifact": None}


def test_update_targets_explicit_thread(isolated_db, capsys):
    from registry import models, view

    actor = mk_session("actor-thr")
    target = mk_session("target-thr")
    record = AR.register_or_resolve(IDENTITY, owner_intent=target)

    rc, out = _run([
        "artifact", "update",
        "--as-session", actor,
        "--artifact-id", record.identity["id"],
        "--thread-id", target,
        "--hide",
    ], capsys)

    assert rc == 0, out
    target_pane = view.pane_for_thread(target)
    assert record.identity["id"] not in {item["id"] for item in target_pane["nodes"]}
    assert target_pane["history"] == []
    assert view.pane_for_thread(actor)["history"] == []
    identity = models.get_artifact(record.identity["id"])
    assert identity["state"] == "active"


def test_reconcile_targets_explicit_thread(isolated_db, capsys, tmp_path):
    from registry import models, view

    actor = mk_session("actor-thr")
    target = mk_session("target-thr")
    missing = tmp_path / "target-only-missing.md"
    artifact = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=str(missing),
        parent_thread_id=target,
        title=missing.name,
        path=str(missing),
    )

    rc, out = _run([
        "artifact", "reconcile",
        "--as-session", actor,
        "--thread-id", target,
    ], capsys)

    assert rc == 0, out
    assert [action["artifact_id"] for action in json.loads(out)["actions"]] == [
        artifact["id"],
    ]
    assert view.reconcile_for_thread(actor)["actions"] == []
