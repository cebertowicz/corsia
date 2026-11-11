from __future__ import annotations

import pytest


def test_active_effect_blocks_repo_transition():
    from infra.repo_policy_transition import (
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    with admit_repo_effect():
        with pytest.raises(RepoPolicyTransitionBusy, match="tool effect"):
            with repo_policy_transition(
                active_process_reaches=lambda: (),
            ):
                pass

    with repo_policy_transition(
        active_process_reaches=lambda: (),
    ):
        pass


def test_transition_blocks_conflicting_admission_and_clears_after_failure():
    from infra.repo_policy_transition import (
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    with pytest.raises(RuntimeError, match="mutation failed"):
        with repo_policy_transition(
            active_process_reaches=lambda: (),
        ):
            with pytest.raises(
                RepoPolicyTransitionBusy,
                match="policy transition",
            ):
                with admit_repo_effect():
                    pass
            raise RuntimeError("mutation failed")

    with admit_repo_effect():
        pass


def test_owned_process_blocks_transition_without_leaking_quarantine():
    from infra.repo_policy_transition import (
        EVERYWHERE,
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    with pytest.raises(RepoPolicyTransitionBusy, match="owned process"):
        with repo_policy_transition(
            active_process_reaches=lambda: [EVERYWHERE],
        ):
            pass

    with admit_repo_effect():
        pass


def test_effect_in_another_project_does_not_block_transition():
    from infra.repo_policy_transition import Scope, admit_repo_effect, repo_policy_transition

    with admit_repo_effect(Scope(frozenset({"q"}))):
        with repo_policy_transition(
            claim=Scope(frozenset({"p"})),
            active_process_reaches=lambda: [Scope(frozenset({"q"}))],
        ):
            # An unrelated project's effect is still admitted mid-transition.
            with admit_repo_effect(Scope(frozenset({"q"}))):
                pass


@pytest.mark.parametrize("reach", ["same project", "everywhere"])
def test_effect_that_can_meet_the_claim_blocks_transition(reach):
    from infra.repo_policy_transition import (
        EVERYWHERE,
        RepoPolicyTransitionBusy,
        Scope,
        admit_repo_effect,
        repo_policy_transition,
    )

    held = EVERYWHERE if reach == "everywhere" else Scope(frozenset({"p", "q"}))
    claim = Scope(frozenset({"p"}))
    with admit_repo_effect(held):
        with pytest.raises(RepoPolicyTransitionBusy, match="tool effect"):
            with repo_policy_transition(claim=claim, active_process_reaches=lambda: ()):
                pass
    with repo_policy_transition(claim=claim, active_process_reaches=lambda: ()):
        with pytest.raises(RepoPolicyTransitionBusy, match="policy transition"):
            with admit_repo_effect(held):
                pass


def test_process_in_claimed_project_blocks_but_other_project_does_not():
    from infra.repo_policy_transition import RepoPolicyTransitionBusy, Scope, repo_policy_transition

    claim = Scope(frozenset({"p"}))
    with pytest.raises(RepoPolicyTransitionBusy, match="owned process"):
        with repo_policy_transition(claim=claim, active_process_reaches=lambda: [Scope(frozenset({"p"}))]):
            pass
    with repo_policy_transition(claim=claim, active_process_reaches=lambda: [Scope(frozenset({"q"}))]):
        pass


def test_unreadable_process_activity_blocks():
    from infra.repo_policy_transition import RepoPolicyTransitionBusy, Scope, repo_policy_transition

    def unreadable():
        raise OSError("gone")

    for reaches in (unreadable, lambda: [1]):
        with pytest.raises(RepoPolicyTransitionBusy, match="could not be verified"):
            with repo_policy_transition(claim=Scope(frozenset({"p"})), active_process_reaches=reaches):
                pass


def _fence_db(tmp_path):
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, path TEXT);
        CREATE TABLE threads (id TEXT PRIMARY KEY, project_id TEXT, local_only INTEGER DEFAULT 0);
        CREATE TABLE project_repo_policies (project_id TEXT, repo_path TEXT);
        CREATE TABLE project_inference_policies (project_id TEXT, cloud_models_off INTEGER);
        """
    )
    workspace = tmp_path / "ws"
    for name in ("p", "q"):
        folder = workspace / "projects" / name
        folder.mkdir(parents=True)
        db.execute("INSERT INTO projects (id, name, path) VALUES (?, ?, ?)", (name, name, str(folder)))
    db.execute("INSERT INTO threads (id, project_id) VALUES ('tq', 'q')")
    return db, workspace


def test_caller_reach_follows_the_project_fence(tmp_path, monkeypatch):
    from infra import corsia_agent
    from infra.repo_policy_transition import EVERYWHERE, Scope, caller_reach

    monkeypatch.setattr(corsia_agent, "get_corsia_agent", lambda db=None: None)
    db, _workspace = _fence_db(tmp_path)

    assert caller_reach("tq", leased=False, db=db) == Scope(frozenset({"q"}))
    assert caller_reach("tq", leased=True, db=db) == EVERYWHERE
    assert caller_reach(None, leased=False, db=db) == EVERYWHERE
    assert caller_reach("missing", leased=False, db=db) == EVERYWHERE


def test_corsia_agent_reach_is_every_project_it_is_not_fenced_from(tmp_path, monkeypatch):
    from infra import corsia_agent
    from infra.repo_policy_transition import Scope, caller_reach

    monkeypatch.setattr(corsia_agent, "get_corsia_agent", lambda db=None: "agent")
    db, _workspace = _fence_db(tmp_path)
    db.execute("INSERT INTO threads (id, project_id) VALUES ('agent', NULL)")
    db.execute("INSERT INTO project_inference_policies VALUES ('q', 1)")

    assert caller_reach("agent", leased=False, db=db) == Scope(frozenset({"p"}))


def test_repo_claim_names_every_project_holding_the_path(tmp_path, monkeypatch):
    from pathlib import Path

    from core import project_context
    from infra.repo_policy_transition import EVERYWHERE, Scope, repo_claim

    db, workspace = _fence_db(tmp_path)
    monkeypatch.setattr(
        project_context,
        "resolve_project_row_paths",
        lambda _ws, *, project_name, raw_path, selection=None: (project_name, Path(raw_path)),
    )
    outside = tmp_path / "code" / "app"
    outside.mkdir(parents=True)
    p_only, p_and_q = Scope(frozenset({"p"})), Scope(frozenset({"p", "q"}))

    # Held by no project and outside the workspace: only the project it joins.
    assert repo_claim("p", outside, db=db, workspace=workspace) == p_only
    # A new project's link claims no existing project.
    assert repo_claim(None, outside, db=db, workspace=workspace) == Scope()
    # Inside another project's folder: that project too.
    assert repo_claim("p", workspace / "projects" / "q" / "lib", db=db, workspace=workspace) == p_and_q
    # Created in the project's own source folder: only that project.
    assert repo_claim("p", workspace / "projects" / "p" / "source" / "new", db=db, workspace=workspace) == p_only
    # Registered to q already, or a parent of q's repo: q too.
    db.execute("INSERT INTO project_repo_policies VALUES ('q', ?)", (str(outside),))
    assert repo_claim("p", outside, db=db, workspace=workspace) == p_and_q
    assert repo_claim("p", outside.parent, db=db, workspace=workspace) == p_and_q
    # Inside the workspace but in no project: every agent can write it today.
    assert repo_claim("p", workspace / "shared" / "x", db=db, workspace=workspace) == EVERYWHERE
    # An unreadable row fails closed.
    db.execute("INSERT INTO project_repo_policies VALUES ('q', 'relative/path')")
    assert repo_claim("p", tmp_path / "other", db=db, workspace=workspace) == EVERYWHERE
