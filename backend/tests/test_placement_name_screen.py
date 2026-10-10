"""Staging and the worker screen a proposal against the same known identities (#2818).

Both ask ``thread_user_names``, so what one refuses the other refuses. A thread
with a falsy user id names nobody (``threads.user_id`` is NOT NULL, so empty is
the falsy value a row can hold).
"""

from __future__ import annotations

import pytest

from tests.test_checkpoint_placement import (  # noqa: F401 -- worker_env is a fixture
    _add,
    _drain,
    _lesson,
    _mark,
    _raw,
    worker_env,
)

_RULE = "None of the commits in a change should be large."


@pytest.mark.parametrize("user_id", [""])
def test_a_thread_without_a_user_id_names_nobody(worker_env, user_id) -> None:
    from core.placement_proposal import thread_user_names

    _, db, _, _, _, _ = worker_env
    db.execute("UPDATE threads SET user_id=? WHERE id='thread-1'", (user_id,))
    db.commit()

    assert thread_user_names(db, "thread-1") == []
    assert thread_user_names(db, "no-such-thread") == []


def test_a_thread_with_a_user_id_names_it(worker_env) -> None:
    from core.placement_proposal import thread_user_names

    _, db, _, _, _, _ = worker_env

    assert thread_user_names(db, "thread-1") == ["user-1"]


@pytest.mark.parametrize("user_id", [""])
async def test_staging_and_the_worker_agree_for_a_falsy_user_id(
    worker_env, user_id,
) -> None:
    from core.placement_proposal import ProposalResult
    from tools.continuity import _live_operation

    worker, db, _, _, _, monkeypatch = worker_env
    db.execute("UPDATE threads SET user_id=? WHERE id='thread-1'", (user_id,))
    db.commit()
    entry = _add(db, "decisions", {**_lesson("prefer small commits"), "rationale": "r"})
    filed: list[dict] = []

    def file_issue(**kwargs) -> ProposalResult:
        filed.append(kwargs)
        return ProposalResult("proposed", "proposed", "https://x/1")

    monkeypatch.setattr(
        "core.placement_proposal.backlog_target",
        lambda db, project_id: ("lane:context", "owner/repo"),
    )
    monkeypatch.setattr("core.placement_proposal.file_issue", file_issue)
    monkeypatch.setattr("core.placement_proposal.resolvable_ref", lambda *a, **k: True)

    # Staging accepts the rule ...
    _live_operation(db, "thread-1", "place", {
        "entry_id": entry, "verdict": "proposed",
        "placement": _raw(q1="project", text=_RULE),
    })
    db.commit()
    await _drain(worker)

    # ... and the worker files it instead of settling it unplaced/p3-verbatim.
    assert [issue["rule"] for issue in filed] == [_RULE]
    assert _mark(db, entry)["placement_state"] == "proposed"


async def test_a_widened_identity_is_refused_at_staging_and_by_the_worker(
    worker_env,
) -> None:
    """Change the one lookup and both sides follow it."""
    from infra.continuity_store import ContinuityError
    from tools.continuity import _live_operation

    worker, db, _, _, _, monkeypatch = worker_env
    entry = _add(db, "decisions", {**_lesson("prefer small commits"), "rationale": "r"})
    rule = "Ask Whitfield before splitting a change."
    monkeypatch.setattr(
        "core.placement_proposal.thread_user_names", lambda db, thread_id: ["Whitfield"],
    )

    with pytest.raises(ContinuityError, match="it names whitfield"):
        _live_operation(db, "thread-1", "place", {
            "entry_id": entry, "verdict": "proposed",
            "placement": _raw(q1="project", text=rule),
        })

    # The worker, handed the same answer directly, settles it unplaced.
    from core.placement_decide import validate_decision
    from core.placement_outcomes import propose
    from types import SimpleNamespace

    settled: list[tuple] = []
    monkeypatch.setattr("core.placement_outcomes.settle", lambda *a, **k: settled.append(a))
    monkeypatch.setattr("core.placement_proposal.resolvable_ref", lambda *a, **k: True)
    monkeypatch.setattr(
        "core.placement_proposal.backlog_target", lambda db, pid: ("l", "o/r"),
    )
    live = {"thread_id": "thread-1", "entry_id": entry, "revision": 1,
            "body": _lesson("prefer small commits")}
    decision = validate_decision(_raw(q1="project", text=rule))
    propose(live, SimpleNamespace(project_id=None), decision)  # type: ignore[arg-type]
    assert [call[1:3] for call in settled] == [("unplaced", "p3-verbatim")]
