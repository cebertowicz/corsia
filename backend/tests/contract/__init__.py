"""The delivery contract as executable invariants.

This package IS the acceptance criteria for the collapse
(PLAN-collapse-delivery-contract.md). Each module encodes one invariant of the
ordered contract as property tests against the real store. A red test here is
a documented contract violation, not a bug in the test: the suite ships red
against the pre-collapse implementation on purpose, and the collapse is done
when every invariant is green and stays green.

Numbering follows the plan:

  I1  one queue                 I6  warm-path checkpoints
  I2  two outcomes              I7  real window only
  I3  one owner                 I8  crash convergence
  I4  countable states          I9  silent-branch ban
  I5  every pause has an exit   I10 UX truth (browser E2E, frontend repo)
"""
