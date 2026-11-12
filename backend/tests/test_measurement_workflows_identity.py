"""The Workflows read answers byte-for-byte as it did before its run-ownership lookup was made linear.

Each digest below was taken from the unchanged read on this seeded data, for every scope and both grains,
so a change that moves a figure, a row, or their order fails here.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from api.measurement_workflows import workflow_runs
from core.correction_attribution import workflow_run_of
from core.measurement_scope import MeasurementScope, resolve_grain
from tests.test_measurement_workflows import NOW, db, measured_owners, seed  # noqa: F401  (fixtures)

SCOPES = {"all": MeasurementScope("all", None), "project": MeasurementScope("alpha", None),
          "root": MeasurementScope("alpha", "root"), "billing": MeasurementScope("alpha", "billing")}
GOLDEN = {
    ("all", "days"): "83be4b42c9feb9f3d003d3ba5aadaf60b69d42147652ea7a0470bb7f808fe824",
    ("all", "weeks"): "88bc85b34aec3da0987cdd567edd20a8f8679df493ca09a145aafd41eacf8d68",
    ("project", "days"): "83be4b42c9feb9f3d003d3ba5aadaf60b69d42147652ea7a0470bb7f808fe824",
    ("project", "weeks"): "88bc85b34aec3da0987cdd567edd20a8f8679df493ca09a145aafd41eacf8d68",
    ("root", "days"): "62d9389e0ed27d6b93232a2d86d22adf3e0692240dcd3119e37c107abbc8cd8f",
    ("root", "weeks"): "9b59f6b5864f0632f41278b49762737bb525474554607487784abba5dd6d321a",
    ("billing", "days"): "a0615e4c11b2be343c9591055a229cd72954dfe8f14eb2168a3ae8c31b19d129",
    ("billing", "weeks"): "6be8537bdb81085d6896b881657ff0bc3b281003eb2f0276a8d1d7df8ca17864",
}


def _digest(scope: MeasurementScope, kind: str) -> str:
    from infra.database import get_db

    connection = get_db()
    try:
        body = workflow_runs(connection, "steve", scope, resolve_grain(kind, "UTC", now=NOW), now=NOW)
    finally:
        connection.close()
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


@pytest.mark.parametrize("kind", ["days", "weeks"])
@pytest.mark.parametrize("name", sorted(SCOPES))
def test_the_workflows_response_is_unchanged_per_scope_and_grain(db, name: str, kind: str) -> None:  # noqa: F811
    seed()
    digest = _digest(SCOPES[name], kind)
    assert digest == GOLDEN[(name, kind)]


def test_run_ownership_is_the_same_for_a_list_and_a_set_of_runs() -> None:
    parents = {"a": None, "b": "a", "c": "b", "d": "c", "x": None, "y": "x", "loop1": "loop2", "loop2": "loop1"}
    runs = ["b", "x"]
    for node in [None, "a", "b", "c", "d", "x", "y", "loop1", "missing"]:
        assert workflow_run_of(node, parents, runs) == workflow_run_of(node, parents, frozenset(runs))
