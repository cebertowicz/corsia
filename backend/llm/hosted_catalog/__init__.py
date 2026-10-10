"""Hosted model catalog — vendored baked snapshot + boot-safe loader.

This package is CORSIA's consumer of the **hosted catalog** (the separate
``corsia-catalog`` data repo: shipped-default model/profile/connector definition
data, ``PRD-HOSTED-MODEL-CATALOG-PROFILES.md``). It holds a **vendored baked
snapshot** of that data (``snapshot/``) plus a general loader that reads it.

Distinct from the ``llm.catalog_*`` modules: those build CORSIA's *live resolved
catalog* (provider-instance health, exact-row dispatch truth). This package is the
*shipped-default definition data* those consumers read from (HMC6).

Contract (HMC2): the snapshot is baked into the repo and **never blocks boot** —
nothing is read at import. The loader reads lazily on first use; a missing or
garbled snapshot fails LOUD with repair guidance at the point of use, it does not
crash startup. Remote-fetch/update of the snapshot is a later slice; today it is
baked-only, re-vendored via ``scripts/sync_catalog_snapshot.py``.

The first consumer is cloud-model reasoning shapes (``cloud_models``); connectors
and model-profiles reuse :func:`loader.load_section` unchanged.
"""
