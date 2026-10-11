"""``CORSIA_DB_POOL_WORKERS`` is a measurement-only override: unset, every DB lane keeps its own size."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from infra import db_async

BACKEND = Path(__file__).resolve().parent.parent
#: The lane sizes the pools had before the override existed: default, read, bulk.
DEFAULT_SIZES = "8 6 2"


def _lane_sizes(env_value: str | None) -> str:
    """Pool sizes in a fresh interpreter, so module-level construction sees ``env_value``."""
    env = {key: value for key, value in os.environ.items() if key != "CORSIA_DB_POOL_WORKERS"}
    if env_value is not None:
        env["CORSIA_DB_POOL_WORKERS"] = env_value
    env["PYTHONPATH"] = str(BACKEND)
    probe = ("from infra import db_async as d; "
             "print(d._pool._max_workers, d._read_pool._max_workers, d._bulk_pool._max_workers)")
    done = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND, env=env, capture_output=True,
                          text=True, timeout=60, check=True)
    return done.stdout.strip()


def test_unset_leaves_every_lane_at_its_own_size() -> None:
    assert _lane_sizes(None) == DEFAULT_SIZES


def test_empty_value_leaves_every_lane_at_its_own_size() -> None:
    assert _lane_sizes("") == DEFAULT_SIZES


def test_override_sets_every_lane() -> None:
    assert _lane_sizes("1") == "1 1 1"


def test_lane_workers_reads_the_variable_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORSIA_DB_POOL_WORKERS", raising=False)
    assert db_async._lane_workers(8) == 8
    monkeypatch.setenv("CORSIA_DB_POOL_WORKERS", "3")
    assert db_async._lane_workers(8) == 3


def test_a_value_that_is_not_a_number_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORSIA_DB_POOL_WORKERS", "many")
    with pytest.raises(ValueError):
        db_async._lane_workers(8)
