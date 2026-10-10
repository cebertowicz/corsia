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
                active_process_count=lambda: 0,
            ):
                pass

    with repo_policy_transition(
        active_process_count=lambda: 0,
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
            active_process_count=lambda: 0,
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
        RepoPolicyTransitionBusy,
        admit_repo_effect,
        repo_policy_transition,
    )

    with pytest.raises(RepoPolicyTransitionBusy, match="owned process"):
        with repo_policy_transition(
            active_process_count=lambda: 1,
        ):
            pass

    with admit_repo_effect():
        pass
