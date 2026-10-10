"""Hierarchical pane projection with a stale-client ``history: []`` shim.

Flat needs/active/earlier lanes and subagent synthesis are retired by design
(artifact-pane-hierarchy). See ``registry/view.py``, ``registry/reconcile.py``,
``registry/presentation.py``.
"""

from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from infra.database import get_db  # noqa: E402
from registry import models, pinning, reconcile, view  # noqa: E402
from tests.registry_helpers import make_session  # noqa: E402


def _mk_parent(db) -> str:
    return make_session(db, prefix="pane-parent")


def _node_ids(pane: dict) -> set[str]:
    return {n["id"] for n in pane["nodes"] if n["kind"] == "artifact"}


class TestHierarchicalPaneShape:
    def test_visible_artifact_is_an_artifact_node_with_presentation(self):
        db = get_db()
        parent = _mk_parent(db)
        art = models.register_artifact(
            integration="workspace",
            type="document",
            external_id=f"doc-{uuid.uuid4().hex[:8]}",
            parent_thread_id=parent,
            title="notes.md",
            db=db,
        )

        pane = view.pane_for_thread(parent)

        assert set(pane) == {"nodes", "needs_attention", "history"}
        item = next(n for n in pane["nodes"] if n["id"] == art["id"])
        assert item["kind"] == "artifact"
        assert item["retention"] == "durable"
        assert item["surfacing"] == "visible"
        assert item["presentation"] == {
            "label": "Document", "icon": "file-text", "capability": "leaf-package",
        }
        assert item["action"] == {
            "capability": "leaf-package", "disabled": False, "disabled_reason": None,
        }
        assert pane["needs_attention"] == []
        assert pane["history"] == []

    def test_pane_payload_reports_owner_pin_state(self):
        db = get_db()
        parent = _mk_parent(db)
        pinned = models.register_artifact(
            integration="workspace",
            type="document",
            external_id=f"pinned-{uuid.uuid4().hex[:8]}",
            parent_thread_id=parent,
            db=db,
        )
        unpinned = models.register_artifact(
            integration="workspace",
            type="document",
            external_id=f"unpinned-{uuid.uuid4().hex[:8]}",
            parent_thread_id=parent,
            db=db,
        )
        pinning.update_pinned(
            pinned["id"], pinned=True, parent_thread_id=parent, db=db,
        )

        nodes = {
            node["id"]: node
            for node in view.pane_for_thread(parent)["nodes"]
            if node["kind"] == "artifact"
        }

        assert nodes[pinned["id"]]["pinned"] is True
        assert nodes[unpinned["id"]]["pinned"] is False

    def test_unsupported_type_gets_honest_fallback_not_broken_row(self):
        db = get_db()
        parent = _mk_parent(db)
        art = models.register_artifact(
            integration="some-connector",
            type="mystery-widget",
            external_id=f"widget-{uuid.uuid4().hex[:8]}",
            parent_thread_id=parent,
            title="widget",
            db=db,
        )

        pane = view.pane_for_thread(parent)

        item = next(n for n in pane["nodes"] if n["id"] == art["id"])
        assert item["presentation"]["capability"] == "unsupported"
        assert item["action"]["disabled"] is True
        assert item["action"]["disabled_reason"]

    def test_pull_request_gets_external_link_capability(self):
        db = get_db()
        parent = _mk_parent(db)
        art = models.register_artifact(
            integration="github",
            type="pull-request",
            external_id=f"owner/repo#{uuid.uuid4().hex[:4]}",
            parent_thread_id=parent,
            title="PR #397: fix(qa/llm-reviewer): gate on flag",
            path="https://github.com/owner/repo/pull/397",
            db=db,
        )

        pane = view.pane_for_thread(parent)

        item = next(n for n in pane["nodes"] if n["id"] == art["id"])
        assert item["presentation"]["capability"] == "external-link"
        # PR titles containing '/' must never be mangled as if they were paths.
        assert item["name"] == "PR #397: fix(qa/llm-reviewer): gate on flag"

    def test_claude_code_session_and_codex_thread_never_appear_in_pane(self):
        db = get_db()
        parent = _mk_parent(db)
        models.register_artifact(
            integration="claude-code", type="session",
            external_id=f"cc-{uuid.uuid4().hex[:8]}", parent_thread_id=parent, db=db,
        )
        models.register_artifact(
            integration="codex", type="thread",
            external_id=f"cx-{uuid.uuid4().hex[:8]}", parent_thread_id=parent, db=db,
        )
        doc = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"doc-{uuid.uuid4().hex[:8]}", parent_thread_id=parent, db=db,
        )

        pane = view.pane_for_thread(parent)

        assert _node_ids(pane) == {doc["id"]}

    def test_a_plan_decision_is_never_a_pane_row(self):
        """#1820: a decision is answered in its chip and kept on its plan node."""
        db = get_db()
        parent = _mk_parent(db)
        models.register_artifact(
            integration="local", type="plan-decision",
            external_id=f"decision-{uuid.uuid4().hex[:8]}", parent_thread_id=parent,
            title="Pick the storage shape", db=db,
        )

        assert _node_ids(view.pane_for_thread(parent)) == set()

    def test_quiet_artifact_is_omitted_but_registry_identity_persists(self):
        db = get_db()
        parent = _mk_parent(db)
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"doc-{uuid.uuid4().hex[:8]}", parent_thread_id=parent,
            title="quiet.md", db=db,
        )
        models.update_surfacing(
            art["id"], surfacing=models.SURFACING_QUIET,
            parent_thread_id=parent, db=db,
        )

        pane = view.pane_for_thread(parent)

        assert art["id"] not in _node_ids(pane)
        assert pane["history"] == []
        identity = models.get_artifact(art["id"], db=db)
        assert identity["state"] == "active"
        assert identity["title"] == "quiet.md"

    def test_subscriber_registration_surfacing_is_independent_of_owner(self):
        db = get_db()
        owner = _mk_parent(db)
        subscriber = _mk_parent(db)
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"shared-{uuid.uuid4().hex[:8]}", parent_thread_id=owner,
            title="shared.md", db=db,
        )
        models.register_artifact(
            integration="workspace", type="document",
            external_id=art["external_id"], parent_thread_id=subscriber,
            title="shared.md", db=db,
        )
        models.update_surfacing(
            art["id"], surfacing=models.SURFACING_QUIET,
            parent_thread_id=subscriber, db=db,
        )

        assert art["id"] not in _node_ids(view.pane_for_thread(subscriber))
        assert art["id"] in _node_ids(view.pane_for_thread(owner))

    def test_missing_ephemeral_artifact_is_tombstoned_by_apply_and_disappears(
        self, tmp_path,
    ):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "working" / "ghost.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="ghost.md", path=str(ghost), retention="ephemeral", db=db,
        )

        reconcile.apply_for_thread(parent)
        pane = view.pane_for_thread(parent)

        assert art["id"] not in _node_ids(pane)
        assert pane["needs_attention"] == []
        identity = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert identity["state"] == "tombstone"
        # A tombstone carries no provenance worth surfacing.
        assert pane["history"] == []

    def test_missing_durable_artifact_is_archived_by_apply_and_leaves_pane(
        self, tmp_path,
    ):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "ghost-durable.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="ghost-durable.md", path=str(ghost), db=db,
        )

        reconcile.apply_for_thread(parent)
        pane = view.pane_for_thread(parent)

        assert art["id"] not in _node_ids(pane)
        identity = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert identity["state"] == "archived"

    def test_nodes_sort_by_most_recent_activity(self):
        db = get_db()
        parent = _mk_parent(db)
        older = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"older-{uuid.uuid4().hex[:8]}", parent_thread_id=parent,
            title="older.md", db=db,
        )
        db.execute(
            "UPDATE artifact_identities SET updated_at='2020-01-01T00:00:00+00:00' "
            "WHERE id=?",
            (older["id"],),
        )
        db.execute(
            "UPDATE artifact_registrations SET updated_at='2020-01-01T00:00:00+00:00' "
            "WHERE artifact_id=?",
            (older["id"],),
        )
        db.commit()
        newer = models.register_artifact(
            integration="workspace", type="document",
            external_id=f"newer-{uuid.uuid4().hex[:8]}", parent_thread_id=parent,
            title="newer.md", db=db,
        )

        pane = view.pane_for_thread(parent)

        ids = [n["id"] for n in pane["nodes"] if n["kind"] == "artifact"]
        assert ids.index(newer["id"]) < ids.index(older["id"])


class TestPaneReadIsSideEffectFree:
    """A GET must never mutate. Repair is scheduled after the response
    (``api.registry_pane._reconcile_after_response``), so the pane projection
    neither stats the filesystem nor retires an identity.
    """

    def test_reading_the_pane_never_repairs_a_missing_artifact(self, tmp_path):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "ghost.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="ghost.md", path=str(ghost), retention="ephemeral", db=db,
        )

        pane = view.pane_for_thread(parent)

        assert art["id"] in _node_ids(pane)
        state = db.execute(
            "SELECT state FROM artifact_identities WHERE id=?", (art["id"],),
        ).fetchone()
        assert state["state"] == "active"
        receipts = db.execute(
            "SELECT COUNT(*) c FROM artifact_reconcile_receipts WHERE artifact_id=?",
            (art["id"],),
        ).fetchone()
        assert receipts["c"] == 0

    def test_needs_attention_is_read_from_stored_failed_receipts(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "denied.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="denied.md", path=str(ghost), db=db,
        )
        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == str(ghost):
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
        reconcile.apply_for_thread(parent)
        monkeypatch.setattr(reconcile.os, "stat", real_stat)

        pane = view.pane_for_thread(parent)

        assert [i["artifact_id"] for i in pane["needs_attention"]] == [art["id"]]
        assert pane["needs_attention"][0]["retryable"] is True
        assert "PermissionError" in pane["needs_attention"][0]["reason"]

    def test_needs_attention_clears_once_the_identity_moves_on(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "denied.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="denied.md", path=str(ghost), db=db,
        )
        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == str(ghost):
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
        reconcile.apply_for_thread(parent)
        monkeypatch.setattr(reconcile.os, "stat", real_stat)
        assert view.pane_for_thread(parent)["needs_attention"]

        # The next pass repairs it; the stale failure must not linger.
        reconcile.apply_for_thread(parent)

        pane = view.pane_for_thread(parent)
        assert pane["needs_attention"] == []
        assert art["id"] not in _node_ids(pane)


class TestArchivedProvenancePersistsOutsidePane:
    def test_archived_artifact_identity_remains_available(self, tmp_path):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "ghost-durable.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="ghost-durable.md", path=str(ghost), db=db,
        )

        reconcile.apply_for_thread(parent)
        pane = view.pane_for_thread(parent)

        assert pane["history"] == []
        assert art["id"] not in _node_ids(pane)
        identity = models.get_artifact(art["id"], db=db)
        assert identity["state"] == "archived"
        assert identity["title"] == "ghost-durable.md"

    def test_tombstoned_artifact_leaves_pane_empty(self, tmp_path):
        db = get_db()
        parent = _mk_parent(db)
        ghost = tmp_path / "working" / "ghost.md"
        art = models.register_artifact(
            integration="workspace", type="document",
            external_id=str(ghost), parent_thread_id=parent,
            title="ghost.md", path=str(ghost), retention="ephemeral", db=db,
        )

        reconcile.apply_for_thread(parent)
        pane = view.pane_for_thread(parent)

        assert pane["history"] == []


class TestGroupProjection:
    """Grouping is provider-driven: the pane must carry a novel provider's
    group and aggregate its children's repair status without any Git-specific
    branch in pane control flow.
    """

    @staticmethod
    def _register_provider(monkeypatch, root, label="Fixture group"):
        from registry import grouping

        class _FixtureProvider:
            name = "fixture-connector"

            def group_for(self, path, *, policies, cache):
                if not str(path).startswith(str(root)):
                    return None
                return grouping.GroupAssignment(
                    provider=self.name, key=f"fixture:{root}",
                    label=label, branch="fixture-branch",
                )

        monkeypatch.setattr(grouping, "_PROVIDERS", list(grouping._PROVIDERS))
        grouping.register_provider(_FixtureProvider())

    def test_registered_provider_projects_its_own_group(self, tmp_path, monkeypatch):
        db = get_db()
        parent = _mk_parent(db)
        self._register_provider(monkeypatch, tmp_path)
        first = tmp_path / "a.md"
        second = tmp_path / "b.md"
        for path in (first, second):
            path.write_text("x")
            models.register_artifact(
                integration="workspace", type="document", external_id=str(path),
                parent_thread_id=parent, title=path.name, path=str(path), db=db,
            )

        pane = view.pane_for_thread(parent)

        group = next(n for n in pane["nodes"] if n["kind"] == "worktree-group")
        assert group["key"] == f"fixture:{tmp_path}"
        assert group["label"] == "Fixture group"
        assert group["branch"] == "fixture-branch"
        assert group["file_count"] == 2
        assert group["attention"] is False
        assert {c["name"] for c in group["children"]} == {"a.md", "b.md"}

    def test_group_attention_is_true_when_a_child_needs_attention(
        self, tmp_path, monkeypatch,
    ):
        db = get_db()
        parent = _mk_parent(db)
        self._register_provider(monkeypatch, tmp_path)
        healthy = tmp_path / "ok.md"
        healthy.write_text("x")
        denied = tmp_path / "denied.md"
        denied.write_text("x")
        for path in (healthy, denied):
            models.register_artifact(
                integration="workspace", type="document", external_id=str(path),
                parent_thread_id=parent, title=path.name, path=str(path), db=db,
            )
        real_stat = os.stat

        def _denying_stat(path, *a, **kw):
            if str(path) == str(denied):
                raise PermissionError(f"denied: {path}")
            return real_stat(path, *a, **kw)

        monkeypatch.setattr(reconcile.os, "stat", _denying_stat)
        reconcile.apply_for_thread(parent)
        monkeypatch.setattr(reconcile.os, "stat", real_stat)

        pane = view.pane_for_thread(parent)

        group = next(n for n in pane["nodes"] if n["kind"] == "worktree-group")
        assert group["attention"] is True


class TestSafeName:
    def test_none_returns_fallback(self):
        assert view._safe_name(None, "fallback") == "fallback"

    def test_empty_string_returns_fallback(self):
        assert view._safe_name("", "fallback") == "fallback"

    def test_plain_title_passes_through(self):
        assert view._safe_name("PR #42 fix something", "fallback") == "PR #42 fix something"

    def test_uuid_v4_returns_fallback(self):
        assert view._safe_name("019ef5c7-0cb4-4a53-9bb6-3b2fe3f10abc", "fallback") == "fallback"

    def test_uuid_v7_returns_fallback(self):
        assert view._safe_name("019ef5c7-0cb4-7a53-9bb6-3b2fe3f10abc", "fallback") == "fallback"

    def test_uuid_prefix_with_suffix_returns_fallback(self):
        assert view._safe_name("019ef5c7-0cb4-7a53-9bb6-3b2fe3f10abc/extra", "fallback") == "fallback"

    def test_absolute_path_returns_basename(self):
        assert view._safe_name("/Users/alice/docs/report.md", "fallback") == "report.md"

    def test_home_relative_path_returns_basename(self):
        assert view._safe_name("~/projects/corsia/README.md", "fallback") == "README.md"

    def test_path_trailing_slash_returns_basename(self):
        assert view._safe_name("/Users/alice/docs/", "fallback") == "docs"


class TestTerminalMarkers:
    async def test_result_marker_is_not_display_content(self):
        from core.message_display import llm_to_display
        from tests.registry_helpers import cc_child

        db = get_db()
        parent = _mk_parent(db)
        child, on_event = cc_child(db, parent, title="fix-the-tests")
        await on_event({
            "kind": "assistant_step", "text": "the answer",
            "reasoning": "", "tool_uses": [],
        })
        await on_event({"kind": "result", "result": "the answer", "is_error": False})

        rows = [dict(r) for r in db.execute(
            "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at, id",
            (child,)).fetchall()]
        display = llm_to_display(rows)
        import json as _json
        sys_types = [(_json.loads(d["metadata"]) if isinstance(d.get("metadata"), str)
                      else d.get("metadata") or {}).get("type")
                     for d in display if d.get("role") == "system"]
        assert "result" not in sys_types
        assert sum(1 for d in display
                   if d.get("role") == "assistant" and d["content"] == "the answer") == 1


class TestGitWorktreePane:
    def test_pane_groups_verified_worktree_without_git_subprocess(
        self, tmp_path, monkeypatch,
    ):
        """The pane reads the branch only after Git worktree verification."""
        import subprocess

        import infra.project_repo_policy as repo_policy
        from infra.project_repo_policy import RepoPolicy

        repo = tmp_path / "repo"
        repo.mkdir()
        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.email", "pane@example.invalid"),
            ("config", "user.name", "Pane"),
        ):
            subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)
        (repo / "README.md").write_text("fixture\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=str(repo), check=True,
        )
        root = repo / ".worktrees" / "pane"
        root.parent.mkdir()
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feat/pane", str(root)],
            cwd=str(repo), check=True,
        )
        target = root / "artifact.md"
        target.write_text("fixture\n")
        policy = RepoPolicy("p1", "repo", repo, None, False)
        monkeypatch.setattr(view, "list_registered_repo_policies", lambda **_kwargs: [policy])

        def unexpected_git_branch_read(*_args, **_kwargs):
            raise AssertionError("pane must not spawn git to read a verified worktree branch")

        monkeypatch.setattr(repo_policy, "_run_git", unexpected_git_branch_read)
        monkeypatch.setattr(subprocess, "Popen", unexpected_git_branch_read)
        monkeypatch.setattr(subprocess, "run", unexpected_git_branch_read)
        db = get_db()
        parent = _mk_parent(db)
        artifact = models.register_artifact(
            integration="workspace", type="document", external_id="pane-worktree",
            parent_thread_id=parent, path=str(target), db=db,
        )

        pane = view.pane_for_thread(parent)

        group = next(node for node in pane["nodes"] if node["kind"] == "worktree-group")
        assert group["branch"] == "feat/pane"
        assert group["children"][0]["id"] == artifact["id"]
