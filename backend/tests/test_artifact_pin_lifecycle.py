"""Pin lifecycle contracts raised by review of PR #1782.

A user pin must survive the agent re-registering the same file (the
registration upsert must not clobber ``owner-pinned``). Unpinning a row that
its originating node settlement had retired must return it to that settled
state (no dead-end persistence) — read straight from the row's own
``retired_at``, which pin never touches. Settlement stamps that column
on a pinned row too (it just leaves surfacing alone), regardless of whether
pin was applied before or after the task settled -- otherwise the fact that
the task finished is lost forever, since settlement fires at most once per
node. A genuine re-registration clears that column (``identity_bridge``), so
unpin after a re-write never resettles a row whose current content is
unrelated to the old retirement. An exempt row (a document the user commented
on) must stay visible regardless of pin toggles.
"""

from __future__ import annotations

import pytest

from infra import config
from infra.database import get_db
from plans import service
from registry import models, pinning, view
from registry.node_retirement import retire_registrations_for_node
from registry.registration_writer import RegistrationOrigin
from tests.registry_helpers import make_session


@pytest.fixture()
def db(isolated_db):
    return get_db()


def _thread_node_artifact(db, tag: str, path: str = ".threads/t1/working/out.md"):
    thread_id = make_session(db, prefix=f"pin-life-{tag}")
    plan = service.ensure_plan("thread", thread_id, db=db)
    node = service.add_node(
        plan.id,
        kind="task",
        title=f"{tag} work",
        acceptance_criteria="Artifact lifecycle settles",
        db=db,
    )
    node = service.transition_node(node.id, "active", db=db)
    turn_id = f"turn-{tag}-{thread_id}"
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch,
            plan_id,plan_node_id,plan_attempt_id,plan_commitment_id,plan_usage_role)
           VALUES (?,?,0,'native/test','running','t0','boot',?,?,?,?,?)""",
        (
            turn_id, thread_id, plan.id, node.id,
            f"attempt-{turn_id}", node.commitment_id, "execution",
        ),
    )
    db.commit()
    artifact = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=path,
        parent_thread_id=thread_id,
        title=path.rsplit("/", 1)[-1],
        origin=RegistrationOrigin(thread_id, turn_id, "artifact_tool"),
        db=db,
    )
    return thread_id, node, artifact


def _row(db, artifact_id: str, thread_id: str):
    return db.execute(
        """SELECT relationship_state, surfacing, retired_at, origin_plan_node_id
           FROM artifact_registrations
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact_id, thread_id),
    ).fetchone()


def _reregister_same_path(db, thread_id: str, turn_suffix: str) -> None:
    """Re-register the same path+thread under a fresh Turn, as the owning agent would."""
    turn = f"turn-{turn_suffix}-{thread_id}"
    db.execute("DELETE FROM active_turns WHERE thread_id=?", (thread_id,))
    db.execute(
        """INSERT INTO active_turns
           (id,thread_id,execution_epoch,model,status,started_at,boot_epoch)
           VALUES (?,?,0,'native/test','running','t1','boot')""",
        (turn, thread_id),
    )
    db.commit()
    models.register_artifact(
        integration="workspace",
        type="document",
        external_id=".threads/t1/working/out.md",
        parent_thread_id=thread_id,
        title="out.md",
        origin=RegistrationOrigin(thread_id, turn, "artifact_tool"),
        db=db,
    )


def test_re_registration_clears_retirement_so_unpin_does_not_resettle(db):
    thread_id, node, artifact = _thread_node_artifact(db, "rereg")
    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None
    )
    assert retired == 1
    assert _row(db, artifact["id"], thread_id)["retired_at"]

    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)

    # The agent writes the same file again under a new turn: the registration
    # upsert must keep the user's pin instead of silently downgrading it, and
    # it clears retired_at because the row is genuinely live again.
    _reregister_same_path(db, thread_id, "rereg2")
    assert _row(db, artifact["id"], thread_id)["relationship_state"] == "owner-pinned"
    assert _row(db, artifact["id"], thread_id)["retired_at"] is None

    # The user can still unpin afterwards; the pin is not sticky against
    # intent. Nothing is retired anymore on this row, so unpin is a pure
    # flag flip rather than resurrecting the old node's settlement.
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    restored = _row(db, artifact["id"], thread_id)
    assert restored["relationship_state"] == "owner-active"
    assert restored["surfacing"] != "settled"
    assert restored["retired_at"] is None


def test_pin_before_settlement_still_stamps_retirement_for_later_unpin(db):
    """The exact reviewer-named gap: pinning before the task settles must
    not lose the fact that it settled. Settlement still stamps
    retired_at on a pinned row -- it just leaves surfacing alone, since
    pin's whole job is to keep the row visible regardless. Unpin then finds
    the fact waiting instead of the row staying visible forever.
    """
    thread_id, node, artifact = _thread_node_artifact(db, "pin-before-settle")
    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)

    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None
    )
    assert retired == 1
    settled_while_pinned = _row(db, artifact["id"], thread_id)
    assert settled_while_pinned["relationship_state"] == "owner-pinned"
    assert settled_while_pinned["surfacing"] == "visible"
    stamp = settled_while_pinned["retired_at"]
    assert stamp

    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    row = _row(db, artifact["id"], thread_id)
    assert row["relationship_state"] == "owner-active"
    assert row["surfacing"] == "settled"
    assert row["retired_at"] == stamp


def test_unpin_after_rereg_does_not_resettle_a_pin_before_settle_stamp(db):
    """A row pinned before its task settles, then genuinely re-registered
    before ever being unpinned: the re-registration clears the retirement
    settlement recorded while it was pinned, so unpin has nothing to
    restore.
    """
    thread_id, node, artifact = _thread_node_artifact(db, "stale-stamp")
    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    assert _row(db, artifact["id"], thread_id)["retired_at"] is None

    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None
    )
    assert retired == 1
    assert _row(db, artifact["id"], thread_id)["retired_at"]

    _reregister_same_path(db, thread_id, "stale-stamp2")
    assert _row(db, artifact["id"], thread_id)["retired_at"] is None

    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    row = _row(db, artifact["id"], thread_id)
    assert row["surfacing"] != "settled"
    assert row["retired_at"] is None


def test_unpin_restores_settlement_the_pin_had_lifted(db):
    thread_id, node, artifact = _thread_node_artifact(db, "resettle")
    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None
    )
    assert retired == 1
    settled = _row(db, artifact["id"], thread_id)
    assert settled["surfacing"] == "settled"
    stamp = settled["retired_at"]
    assert stamp

    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    lifted = _row(db, artifact["id"], thread_id)
    # Pin only unhides the row; retired_at is the permanent fact of
    # when settlement retired it, and pin never touches it.
    assert (lifted["surfacing"], lifted["retired_at"]) == ("visible", stamp)

    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    restored = _row(db, artifact["id"], thread_id)
    assert restored["relationship_state"] == "owner-active"
    assert restored["surfacing"] == "settled"
    assert restored["retired_at"] == stamp


def test_toggling_pin_again_cannot_resurrect_a_cleared_retirement(db):
    """Once a re-registration clears retired_at, re-pinning and
    unpinning again must not somehow bring the old settlement back."""
    thread_id, node, artifact = _thread_node_artifact(db, "retoggle")
    retired = retire_registrations_for_node(
        db, node_id=node.id, trigger="accepted", causal_event_id=None
    )
    assert retired == 1

    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    _reregister_same_path(db, thread_id, "retoggle2")
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    first_unpin = _row(db, artifact["id"], thread_id)
    assert first_unpin["surfacing"] != "settled"
    assert first_unpin["retired_at"] is None

    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    row = _row(db, artifact["id"], thread_id)
    assert row["surfacing"] != "settled"
    assert row["retired_at"] is None


def test_unpin_keeps_rows_never_retired_visible(db):
    thread_id, _node, artifact = _thread_node_artifact(db, "active")
    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    row = _row(db, artifact["id"], thread_id)
    assert (row["surfacing"], row["retired_at"]) == ("visible", None)


def _user_comment(db, artifact_id: str) -> None:
    db.execute(
        "INSERT INTO doc_comments "
        "(id,artifact_id,author,author_kind,body,created_at,updated_at) "
        "VALUES (?,?,'user','user','Check this.','t0','t0')",
        (f"c-{artifact_id}", artifact_id),
    )


def test_exempt_commented_document_row_stays_visible_across_pin_toggles(db):
    thread_id, _node, artifact = _thread_node_artifact(
        db, "exempt", path="workspace://ws-1/shared/planning/out.md",
    )
    # An exempt row that was settled before the exemption applied to it.
    _user_comment(db, artifact["id"])
    db.execute(
        """UPDATE artifact_identities
           SET retention='durable', path=?,
               metadata=json_set(COALESCE(metadata,'{}'), '$.zone', 'workspace')
           WHERE id=?""",
        (str(config.WORKSPACE / "shared/planning/out.md"), artifact["id"]),
    )
    db.execute(
        """UPDATE artifact_registrations
           SET surfacing='settled', retired_at='2026-09-10T00:00:00+00:00'
           WHERE artifact_id=? AND parent_thread_id=?""",
        (artifact["id"], thread_id),
    )
    db.commit()

    pinning.update_pinned(artifact["id"], pinned=True, parent_thread_id=thread_id, db=db)
    assert _row(db, artifact["id"], thread_id)["surfacing"] == "visible"

    # Unpinning must NOT re-retire: the user's comment exempts the document.
    # Pin/unpin never mutates retired_at (only node settlement and
    # re-registration do), so the legacy stamp is left exactly as it was —
    # it's simply inert against an exempt row in every consumer.
    pinning.update_pinned(artifact["id"], pinned=False, parent_thread_id=thread_id, db=db)
    row = _row(db, artifact["id"], thread_id)
    assert row["surfacing"] == "visible"
    assert row["retired_at"] == "2026-09-10T00:00:00+00:00"


def _register_durable(
    db, thread_id: str, tag: str, zone: str, folder: str = "shared/planning",
) -> dict:
    artifact = models.register_artifact(
        integration="workspace",
        type="document",
        external_id=f"workspace://ws-1/{folder}/{tag}.md",
        # Shared is read from where the file is stored, not from the identity.
        path=str(config.WORKSPACE / folder / f"{tag}.md"),
        parent_thread_id=thread_id,
        title=f"{tag}.md",
        retention="durable",
        metadata={"zone": zone},
        db=db,
    )
    return artifact


def test_pane_retirement_exempt_flag_matches_the_shared_sql_predicate(db):
    """registry.view's retirement_exempt equals membership in the same
    NOT-exempt set node settlement and unpin both use — one predicate."""
    thread_id = make_session(db, prefix="view-exempt")
    workspace = _register_durable(db, thread_id, "ws", "workspace")
    own_folder = _register_durable(db, thread_id, "own", "workspace", ".threads/t1")
    repo = _register_durable(db, thread_id, "repo", "repo")
    pinned_ws = _register_durable(db, thread_id, "pinned", "workspace")
    commented = _register_durable(db, thread_id, "commented", "workspace")
    pinning.update_pinned(pinned_ws["id"], pinned=True, parent_thread_id=thread_id, db=db)
    _user_comment(db, commented["id"])
    db.commit()

    items = {n["id"]: n for n in view.pane_for_thread(thread_id)["nodes"] if n["kind"] == "artifact"}
    # A shared document is task-scoped; pin keeps a row but is not an exemption.
    assert items[workspace["id"]]["retirement_exempt"] is False
    assert items[own_folder["id"]]["retirement_exempt"] is False
    assert items[repo["id"]]["retirement_exempt"] is False
    assert items[pinned_ws["id"]]["retirement_exempt"] is False
    assert items[commented["id"]]["retirement_exempt"] is True
