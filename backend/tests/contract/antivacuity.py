"""Anti-vacuity: a scan that finds nothing has proven nothing.

Every scan-based invariant in this suite has the same failure mode, and it is
the most dangerous one available to us: Phase 1 deletes the modules the scan
reads, the scan's input set becomes empty, and the test goes green while
guarding nothing. The suite would report success at exactly the moment the
collapse most needs a judge.

Three independent adversarial reviews of the Phase 1 designs found this in
``test_i4_countable_states.test_checkpoint_states_are_the_contract_set``
(path-filtered to ``context_cutover``, which the collapse deletes), so it is
not hypothetical.

Every scan-based test therefore declares what it must find in order for its
verdict to mean anything. If the evidence disappears, the test fails and says
so, instead of quietly passing.
"""

from __future__ import annotations


class VacuousScan(AssertionError):
    """A contract scan whose input set vanished — the verdict is meaningless."""


def require_evidence(
    found: object,
    *,
    scan: str,
    expectation: str,
    replacement_hint: str,
) -> None:
    """Fail loudly when a scan has nothing to judge.

    ``found`` is whatever the scan collected (any sized or truthy object).
    ``expectation`` states what must exist for the verdict to be real, and
    ``replacement_hint`` tells the future engineer what to re-point the scan at
    once the old implementation is gone — because the honest response to
    "the code I scanned was deleted" is to scan its replacement, never to
    accept the silence as proof.
    """
    size = len(found) if hasattr(found, "__len__") else (1 if found else 0)
    if size:
        return
    raise VacuousScan(
        f"{scan} found no evidence to judge, so its green verdict would be "
        f"meaningless.\nExpected to find: {expectation}\n"
        f"If the implementation moved or was deleted, re-point this scan: "
        f"{replacement_hint}"
    )


def _self_test() -> None:
    """The guard's own passing and failing cases, run at import by the suite.

    A guard shipped without exercising both of its branches is precisely the
    defect the #1002 post-mortem names as root cause #2. This costs
    microseconds and makes that impossible here.
    """
    require_evidence(
        ["something"], scan="self-test", expectation="x", replacement_hint="y",
    )
    for empty in ([], set(), {}, 0, None):
        try:
            require_evidence(
                empty, scan="self-test", expectation="x", replacement_hint="y",
            )
        except VacuousScan:
            continue
        raise AssertionError(
            f"require_evidence accepted empty evidence {empty!r}; the "
            "anti-vacuity guard does not guard"
        )


_self_test()
