"""Derived project source state at the repository mutation's commit boundary."""
from pathlib import Path

from api.project_scaffolding import _write_doc_roots, compute_doc_roots
from events.connectors.github_screen import install
from infra.write_effects import after_commit


def sync_sources(project_id: str, ws_dir: Path, db) -> None:
    """Write doc roots with the mutation; reconcile screening after it commits."""
    _write_doc_roots(db, project_id, compute_doc_roots(ws_dir))
    after_commit(db, lambda: install.sync(db, project_id))
