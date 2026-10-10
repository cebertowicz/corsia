"""The failure the v7->v8 migration raises when data cannot be converted.

Its own module so the lineage and table-mechanics halves of the migration can
share it without either importing the other.
"""

from __future__ import annotations


class PlanMigrationError(RuntimeError):
    """The source database cannot be converted without guessing or data loss."""
