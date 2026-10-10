"""Shared SQL for the External endpoint a ledger row was served by."""

from __future__ import annotations


def server_id_sql(model_column: str) -> str:
    """SQL for the server id in an ``external@<server>/<model>`` column: the text before the first ``/``."""
    return f"substr({model_column},10,instr(substr({model_column},10),'/')-1)"
