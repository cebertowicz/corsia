"""Coordination MVP (S0) — contract fixtures, AC/gate map, and guard ratchets.

S0 introduces NO behavior. It makes S1–S7 measurable:
- ``fixtures`` — the canonical launch scenarios as declared data (the contract
  later slices implement against).
- ``test_guards`` — ratchets that fail if a later slice violates a locked
  invariant (connector-specific router; user-attention class in delivery;
  parent-chain wake).
- ``test_cross_slice_contract`` — the 10-item cross-slice pack as
  xfail-until-implemented scaffolds, one per owning slice.

See ``AC-TEST-MAP.md`` for the AC → test → slice → gate mapping.
"""
