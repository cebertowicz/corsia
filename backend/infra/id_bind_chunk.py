"""The one rule for how many ids one SQL statement may bind.

A page can carry more ids than one statement is allowed to bind, and the
statement then fails with `too many SQL variables`. That ceiling is a
per-connection SQLite limit: it varies by build and can be lowered at runtime,
so a reader that binds one placeholder per id never relies on the number it
sees. It binds at most ``IDS_PER_STATEMENT`` ids per statement and reads the
rest in chunks.

400 keeps every such statement under 999, the ceiling SQLite held long before
any version this project runs, so no page size can push a statement over it.
"""

from __future__ import annotations

from typing import Iterator, Sequence

IDS_PER_STATEMENT = 400


def id_chunks(ids: Sequence[str]) -> Iterator[Sequence[str]]:
    """Yield consecutive slices of at most ``IDS_PER_STATEMENT`` ids."""
    for start in range(0, len(ids), IDS_PER_STATEMENT):
        yield ids[start:start + IDS_PER_STATEMENT]
