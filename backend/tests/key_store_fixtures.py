"""Failure injection for key store rollback tests."""

from __future__ import annotations

import json

from infra import key_store, key_store_values
from infra.db_connection import _NoOpClose


def fail_the_commit_after_a_key_write(patch) -> None:
    """Refuse every commit once a key value has been written; ``patch`` is a monkeypatch context."""
    real_put = key_store_values.put
    wrote: list[str] = []

    def put(conn, name, value):
        wrote.append(name)
        return real_put(conn, name, value)

    def commit(self):
        if wrote:
            raise RuntimeError("commit failed")
        return object.__getattribute__(self, "_conn").commit()

    patch.setattr(key_store_values, "put", put)
    patch.setattr(_NoOpClose, "commit", commit, raising=False)


def values_after_a_sweep() -> dict[str, str]:
    """The values file's contents once a sweep has removed every unreferenced value."""
    key_store_values.sweep()
    path = key_store.keys_dir() / key_store.VALUES_FILE
    return json.loads(path.read_text()) if path.exists() else {}
