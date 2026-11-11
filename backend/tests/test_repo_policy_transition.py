from __future__ import annotations

import threading

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
        UNCONFINED,
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    with pytest.raises(RepoPolicyTransitionBusy, match="owned process"):
        with repo_policy_transition(
            active_process_reaches=lambda: [UNCONFINED],
        ):
            pass

    with admit_repo_effect():
        pass


def _only(project: str):
    """The reach of a caller fenced from every test project but ``project``."""
    from infra.repo_policy_transition import Reach

    return Reach(frozenset({"p", "q"}) - {project})


def test_effect_in_another_project_does_not_block_transition():
    from infra.repo_policy_transition import Claim, admit_repo_effect, repo_policy_transition

    with admit_repo_effect(_only("q")):
        with repo_policy_transition(
            claim=Claim(frozenset({"p"})),
            active_process_reaches=lambda: [_only("q")],
        ):
            # An unrelated project's effect is still admitted mid-transition.
            with admit_repo_effect(_only("q")):
                pass


@pytest.mark.parametrize("reach", ["same project", "unfenced"])
def test_effect_that_can_meet_the_claim_blocks_transition(reach):
    from infra.repo_policy_transition import (
        UNCONFINED,
        Claim,
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    held = UNCONFINED if reach == "unfenced" else _only("p")
    claim = Claim(frozenset({"p"}))
    with admit_repo_effect(held):
        with pytest.raises(RepoPolicyTransitionBusy, match="tool effect"):
            with repo_policy_transition(claim=claim, active_process_reaches=lambda: ()):
                pass
    with repo_policy_transition(claim=claim, active_process_reaches=lambda: ()):
        with pytest.raises(RepoPolicyTransitionBusy, match="policy transition"):
            with admit_repo_effect(held):
                pass


def test_project_created_after_admission_counts_as_reachable():
    from infra.repo_policy_transition import Claim

    # Fenced from p and q when admitted; "new" did not exist yet.
    assert _only("none").meets(Claim(frozenset({"new"})))
    assert not _only("none").meets(Claim(frozenset({"p"})))


def test_only_unconfined_effects_meet_a_claim_on_no_project():
    """A new project linking a folder no project holds: only a call that can write any path meets it."""
    from infra.repo_policy_transition import UNCONFINED, Claim, Reach

    assert UNCONFINED.meets(Claim())
    assert not Reach().meets(Claim())


def test_process_in_claimed_project_blocks_but_other_project_does_not():
    from infra.repo_policy_transition import Claim, RepoPolicyTransitionBusy, repo_policy_transition

    claim = Claim(frozenset({"p"}))
    with pytest.raises(RepoPolicyTransitionBusy, match="owned process"):
        with repo_policy_transition(claim=claim, active_process_reaches=lambda: [_only("p")]):
            pass
    with repo_policy_transition(claim=claim, active_process_reaches=lambda: [_only("q")]):
        pass


def test_unreadable_process_activity_blocks():
    from infra.repo_policy_transition import Claim, RepoPolicyTransitionBusy, repo_policy_transition

    def unreadable():
        raise OSError("gone")

    for reaches in (unreadable, lambda: [1]):
        with pytest.raises(RepoPolicyTransitionBusy, match="could not be verified"):
            with repo_policy_transition(claim=Claim(frozenset({"p"})), active_process_reaches=reaches):
                pass


def test_process_reads_hold_no_lock_but_the_transition_is_already_published():
    """Admission on another thread isn't held up by the process read, yet a meeting effect is refused."""
    from infra.repo_policy_transition import (
        Claim,
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    outcomes: list[str] = []

    def admit(reach) -> None:
        try:
            with admit_repo_effect(reach):
                outcomes.append("admitted")
        except RepoPolicyTransitionBusy:
            outcomes.append("refused")

    def reaches():
        for reach in (_only("q"), _only("p")):
            other = threading.Thread(target=admit, args=(reach,))
            other.start()
            other.join(timeout=5)
            assert not other.is_alive(), "tool admission waited on the process read"
        return ()

    with repo_policy_transition(claim=Claim(frozenset({"p"})), active_process_reaches=reaches):
        pass
    assert outcomes == ["admitted", "refused"]


def test_owned_process_reaches_reads_each_caller_once(monkeypatch):
    from types import SimpleNamespace

    from host_access.processes import PROCESS_OWNER
    from infra import repo_policy_transition

    records = (
        SimpleNamespace(thread_id="a", lease_id=None, permit_id=None),
        SimpleNamespace(thread_id="a", lease_id=None, permit_id=None),
        SimpleNamespace(thread_id="a", lease_id=None, permit_id="permit"),
    )
    reads: list[tuple[str, bool]] = []
    monkeypatch.setattr(PROCESS_OWNER, "outstanding_records", lambda: records)
    monkeypatch.setattr(
        repo_policy_transition, "caller_reach",
        lambda thread_id, *, unconfined: reads.append((thread_id, unconfined)) or repo_policy_transition.UNCONFINED,
    )

    assert len(repo_policy_transition.owned_process_reaches()) == 2
    assert sorted(reads) == [("a", False), ("a", True)]


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
    from infra.repo_policy_transition import UNCONFINED, Reach, caller_reach

    monkeypatch.setattr(corsia_agent, "get_corsia_agent", lambda db=None: None)
    db, _workspace = _fence_db(tmp_path)

    assert caller_reach("tq", unconfined=False, db=db) == Reach(frozenset({"p"}))
    assert caller_reach("tq", unconfined=True, db=db) == UNCONFINED
    assert caller_reach(None, unconfined=False, db=db) == UNCONFINED
    assert caller_reach("missing", unconfined=False, db=db) == UNCONFINED


def test_corsia_agent_reach_is_every_project_it_is_not_fenced_from(tmp_path, monkeypatch):
    from infra import corsia_agent
    from infra.repo_policy_transition import Reach, caller_reach

    monkeypatch.setattr(corsia_agent, "get_corsia_agent", lambda db=None: "agent")
    db, _workspace = _fence_db(tmp_path)
    db.execute("INSERT INTO threads (id, project_id) VALUES ('agent', NULL)")
    db.execute("INSERT INTO project_inference_policies VALUES ('q', 1)")

    assert caller_reach("agent", unconfined=False, db=db) == Reach(frozenset({"q"}))


def test_repo_claim_names_every_project_holding_the_path(tmp_path, monkeypatch):
    from pathlib import Path

    from core import project_context
    from infra.repo_policy_transition import EVERYWHERE, Claim, repo_claim

    db, workspace = _fence_db(tmp_path)
    monkeypatch.setattr(
        project_context,
        "resolve_project_row_paths",
        lambda _ws, *, project_name, raw_path, selection=None: (project_name, Path(raw_path)),
    )
    outside = tmp_path / "code" / "app"
    outside.mkdir(parents=True)
    p_only, p_and_q = Claim(frozenset({"p"})), Claim(frozenset({"p", "q"}))

    # Held by no project and outside the workspace: only the project it joins.
    assert repo_claim("p", outside, db=db, workspace=workspace) == p_only
    # A new project's link claims no existing project.
    assert repo_claim(None, outside, db=db, workspace=workspace) == Claim()
    # Inside another project's folder: that project too.
    assert repo_claim("p", workspace / "projects" / "q" / "lib", db=db, workspace=workspace) == p_and_q
    # Created in the project's own source folder: only that project.
    assert repo_claim("p", workspace / "projects" / "p" / "source" / "new", db=db, workspace=workspace) == p_only
    # Registered to q already, or outside the workspace and a parent of q's repo: q too.
    db.execute("INSERT INTO project_repo_policies VALUES ('q', ?)", (str(outside),))
    assert repo_claim("p", outside, db=db, workspace=workspace) == p_and_q
    assert repo_claim("p", outside.parent, db=db, workspace=workspace) == p_and_q
    # Inside the workspace but in no project: every agent can write it today.
    assert repo_claim("p", workspace / "shared" / "x", db=db, workspace=workspace) == EVERYWHERE
    # Inside the workspace and only partly held: the rest is writable by every agent.
    shared_repo = workspace / "shared" / "foo"
    db.execute("INSERT INTO project_repo_policies VALUES ('q', ?)", (str(shared_repo),))
    assert repo_claim("p", workspace / "shared", db=db, workspace=workspace) == EVERYWHERE
    assert repo_claim("p", shared_repo, db=db, workspace=workspace) == p_and_q
    # An unreadable row fails closed.
    db.execute("INSERT INTO project_repo_policies VALUES ('q', 'relative/path')")
    assert repo_claim("p", tmp_path / "other", db=db, workspace=workspace) == EVERYWHERE
