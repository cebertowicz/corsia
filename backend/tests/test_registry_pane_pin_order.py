"""Pinned-first pane ordering (``registry/view.pane_for_thread``).

A pinned artifact is one the user returns to across tasks, so it leads the
pane instead of sinking under the day's churn. Recency still orders within
each tier. A pinned file inside a worktree group leads its group rather than
being lifted out of it: the group is how those paths are read, and promoting
one child would strand it from the branch it belongs to.

Split from ``test_registry_pane.py``, which is already over the file limit.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra.database import get_db  # noqa: E402
from registry import models, pinning, view  # noqa: E402
from tests.registry_helpers import make_session  # noqa: E402


def _register(db, parent: str, title: str, path: str | None = None) -> dict:
    return models.register_artifact(
        integration="workspace",
        type="document",
        external_id=f"{title}-{uuid.uuid4().hex[:8]}",
        parent_thread_id=parent,
        title=title,
        path=path,
        db=db,
    )


def _age(db, artifact_id: str, stamp: str) -> None:
    """Backdate both identity and registration activity for one artifact."""
    db.execute(
        "UPDATE artifact_identities SET updated_at=? WHERE id=?", (stamp, artifact_id),
    )
    db.execute(
        "UPDATE artifact_registrations SET updated_at=? WHERE artifact_id=?",
        (stamp, artifact_id),
    )
    db.commit()


def _artifact_ids(pane: dict) -> list[str]:
    return [n["id"] for n in pane["nodes"] if n["kind"] == "artifact"]


class TestTopLevelPinnedFirst:
    def test_pinned_row_leads_a_more_recent_unpinned_row(self):
        db = get_db()
        parent = make_session(db, prefix="pin-order")
        pinned = _register(db, parent, "prd.md")
        pinning.update_pinned(pinned["id"], pinned=True, parent_thread_id=parent, db=db)
        _age(db, pinned["id"], "2020-01-01T00:00:00+00:00")
        recent = _register(db, parent, "scratch.md")

        ids = _artifact_ids(view.pane_for_thread(parent))

        assert ids.index(pinned["id"]) < ids.index(recent["id"])

    def test_recency_still_orders_within_the_pinned_tier(self):
        db = get_db()
        parent = make_session(db, prefix="pin-tier")
        older = _register(db, parent, "older-prd.md")
        newer = _register(db, parent, "newer-prd.md")
        for art in (older, newer):
            pinning.update_pinned(art["id"], pinned=True, parent_thread_id=parent, db=db)
        _age(db, older["id"], "2020-01-01T00:00:00+00:00")
        _age(db, newer["id"], "2026-01-01T00:00:00+00:00")

        ids = _artifact_ids(view.pane_for_thread(parent))

        assert ids.index(newer["id"]) < ids.index(older["id"])

    def test_unpinning_returns_the_row_to_recency_order(self):
        db = get_db()
        parent = make_session(db, prefix="pin-release")
        art = _register(db, parent, "was-pinned.md")
        pinning.update_pinned(art["id"], pinned=True, parent_thread_id=parent, db=db)
        _age(db, art["id"], "2020-01-01T00:00:00+00:00")
        recent = _register(db, parent, "fresh.md")
        pinning.update_pinned(art["id"], pinned=False, parent_thread_id=parent, db=db)
        _age(db, art["id"], "2020-01-01T00:00:00+00:00")

        ids = _artifact_ids(view.pane_for_thread(parent))

        assert ids.index(recent["id"]) < ids.index(art["id"])


class TestPinnedInsideWorktreeGroup:
    @staticmethod
    def _register_provider(monkeypatch, root):
        from registry import grouping

        class _FixtureProvider:
            name = "pin-order-connector"

            def group_for(self, path, *, policies, cache):
                if not str(path).startswith(str(root)):
                    return None
                return grouping.GroupAssignment(
                    provider=self.name, key=f"pin-order:{root}",
                    label="Fixture group", branch="fixture-branch",
                )

        monkeypatch.setattr(grouping, "_PROVIDERS", list(grouping._PROVIDERS))
        grouping.register_provider(_FixtureProvider())

    def test_pinned_child_leads_its_group_without_leaving_it(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        parent = make_session(db, prefix="pin-group")
        self._register_provider(monkeypatch, tmp_path)
        pinned = _register(db, parent, "notes.md", path=str(tmp_path / "notes.md"))
        pinning.update_pinned(pinned["id"], pinned=True, parent_thread_id=parent, db=db)
        _age(db, pinned["id"], "2020-01-01T00:00:00+00:00")
        sibling = _register(db, parent, "fresh.md", path=str(tmp_path / "fresh.md"))

        pane = view.pane_for_thread(parent)

        assert pinned["id"] not in _artifact_ids(pane)
        group = next(n for n in pane["nodes"] if n["kind"] == "worktree-group")
        children = [c["id"] for c in group["children"]]
        assert children == [pinned["id"], sibling["id"]]
