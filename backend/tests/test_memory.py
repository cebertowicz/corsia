
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))



class TestContextBuilderSystemPrompt:

    def _make_builder(self, workspace: Path) -> "ContextBuilder":
        from core.context_builder import ContextBuilder
        return ContextBuilder(workspace=workspace)

    def test_system_prompt_contains_runtime_envelope_without_hardcoded_identity(self, tmp_path):
        builder = self._make_builder(tmp_path)
        prompt = builder.build_system_prompt()
        assert "CORSIA Runtime" in prompt
        assert "Jarvis" not in prompt

    def test_system_prompt_contains_workspace_path(self, tmp_path):
        builder = self._make_builder(tmp_path)
        prompt = builder.build_system_prompt()
        assert str(tmp_path.resolve()) in prompt

    def test_bootstrap_files_loaded_when_present(self, tmp_path):
        # Directory-as-truth (Phase 1b): static bootstrap files live in context/.
        (tmp_path / "context").mkdir()
        (tmp_path / "context" / "AGENTS.md").write_text("# Agent Instructions\nBe helpful.")
        (tmp_path / "context" / "USER.md").write_text("# User Info\nName: Steve")
        builder = self._make_builder(tmp_path)
        prompt = builder.build_system_prompt()
        assert "Agent Instructions" in prompt
        assert "User Info" in prompt

    def test_system_context_files_are_the_bootstrap_source_truth(self, tmp_path):
        # Files dropped into the workspace context/ dir are the source truth —
        # discovered by the dir-scan with no hardcoded list or special-case.
        (tmp_path / "context").mkdir()
        (tmp_path / "context" / "CORSIA.md").write_text("# CORSIA\nShip cleanly.")
        (tmp_path / "context" / "AGENTS.md").write_text("# Agents\nFollow the gate.")

        from core.workspace_context_sources import workspace_context_files

        files = workspace_context_files(tmp_path)
        by_name = {f.name: f for f in files}

        assert by_name["context/CORSIA.md"].path == (
            tmp_path / "context" / "CORSIA.md"
        ).resolve()
        assert by_name["context/CORSIA.md"].exists is True
        assert by_name["context/CORSIA.md"].editable is False
        assert by_name["context/CORSIA.md"].owner_surface == "document-viewer"
        assert by_name["context/AGENTS.md"].path == (tmp_path / "context" / "AGENTS.md").resolve()
        assert by_name["context/AGENTS.md"].exists is True
        assert by_name["context/AGENTS.md"].editable is False

    def test_bootstrap_files_absent_when_missing(self, tmp_path):
        builder = self._make_builder(tmp_path)
        prompt = builder.build_system_prompt()
        assert "CORSIA Runtime" in prompt
        assert "## AGENTS.md\n\n" not in prompt
        assert "## USER.md\n\n" not in prompt

    def test_project_context_absent_for_global_conversation(self, tmp_path):
        builder = self._make_builder(tmp_path)
        prompt = builder.build_system_prompt(project_id=None)
        assert "# Project:" not in prompt

    def test_project_context_present_for_scoped_conversation(self, tmp_path):
        project_dir = tmp_path / "projects" / "myproject"
        (project_dir / "context").mkdir(parents=True)
        (project_dir / "context" / "PROJECT.md").write_text("# MyProject\nAgent instructions here.")
        (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")

        builder = self._make_builder(tmp_path)

        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: {
            "name": "myproject", "path": None
        }[key]

        with patch("core.project_context.resolve_project_paths",
                   return_value=("myproject", project_dir)):
            prompt = builder.build_system_prompt(project_id="proj-123")

        assert "# Project: myproject" in prompt
        assert "Agent instructions here." in prompt



class TestContextBuilderProjectContext:

    def _make_builder(self, workspace: Path) -> "ContextBuilder":
        from core.context_builder import ContextBuilder
        return ContextBuilder(workspace=workspace)

    def test_project_md_loaded_from_workspace_dir(self, tmp_path):
        project_dir = tmp_path / "projects" / "alpha"
        (project_dir / "context").mkdir(parents=True)
        (project_dir / "context" / "PROJECT.md").write_text("# Alpha Project\nDo the thing.")
        (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")

        builder = self._make_builder(tmp_path)
        with patch("core.project_context.resolve_project_paths",
                   return_value=("alpha", project_dir)):
            result = builder._load_project_context("proj-alpha")

        assert result is not None
        assert "Alpha Project" in result
        assert "Do the thing." in result

    def test_brief_md_loaded_from_workspace_dir(self, tmp_path):
        project_dir = tmp_path / "projects" / "beta"
        (project_dir / "context").mkdir(parents=True)
        (project_dir / "context" / "PROJECT.md").write_text("# Beta Project")
        (project_dir / "context" / "BRIEF.md").write_text("# Beta Brief\nWhat it is.")
        (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")

        builder = self._make_builder(tmp_path)
        with patch("core.project_context.resolve_project_paths",
                   return_value=("beta", project_dir)):
            result = builder._load_project_context("proj-beta")

        assert result is not None
        assert "Beta Brief" in result

    def test_working_files_are_not_automatically_listed(self, tmp_path):
        project_dir = tmp_path / "projects" / "gamma"
        working_dir = project_dir / "working"
        working_dir.mkdir(parents=True)
        (project_dir / "context").mkdir()
        (working_dir / "BRAIN-DUMP.md").write_text("brain dump content")
        (working_dir / "PLAN.md").write_text("plan content")
        (project_dir / "context" / "PROJECT.md").write_text("# Gamma")
        (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")

        builder = self._make_builder(tmp_path)
        with patch("core.project_context.resolve_project_paths",
                   return_value=("gamma", project_dir)):
            result = builder._load_project_context("proj-gamma")

        assert result is not None
        assert "Working Files" not in result
        assert "BRAIN-DUMP.md" not in result
        assert "PLAN.md" not in result

    def test_missing_required_project_context_fails_loudly(self, tmp_path):
        project_dir = tmp_path / "projects" / "empty"
        project_dir.mkdir(parents=True)

        builder = self._make_builder(tmp_path)
        with patch("core.project_context.resolve_project_paths",
                   return_value=("empty", project_dir)):
            with pytest.raises(RuntimeError, match="missing required project context"):
                builder._load_project_context("proj-empty")

    def test_project_name_in_header(self, tmp_path):
        project_dir = tmp_path / "projects" / "delta"
        (project_dir / "context").mkdir(parents=True)
        (project_dir / "context" / "PROJECT.md").write_text("# Delta\nContent.")
        (project_dir / "context" / "COORDINATION-MODEL.md").write_text("# Coordination")

        builder = self._make_builder(tmp_path)
        with patch("core.project_context.resolve_project_paths",
                   return_value=("delta", project_dir)):
            result = builder._load_project_context("proj-delta")

        assert result is not None
        assert "# Project: delta" in result

class TestContextBuilderMessages:

    def _make_builder(self, workspace: Path) -> "ContextBuilder":
        from core.context_builder import ContextBuilder
        return ContextBuilder(workspace=workspace)

    def test_messages_start_with_system(self, tmp_path):
        builder = self._make_builder(tmp_path)
        messages = builder.build_messages(
            history=[],
            current_message="Hello",
        )
        assert messages[0]["role"] == "system"

    def test_messages_end_with_user(self, tmp_path):
        builder = self._make_builder(tmp_path)
        messages = builder.build_messages(
            history=[],
            current_message="What's up?",
        )
        assert messages[-1]["role"] == "user"

    def test_history_preserved_between_system_and_user(self, tmp_path):
        builder = self._make_builder(tmp_path)
        history = [
            {"role": "user", "content": "First message"},
            {"role": "assistant", "content": "First reply"},
        ]
        messages = builder.build_messages(
            history=history,
            current_message="Second message",
        )
        assert len(messages) == 4
        assert messages[1]["role"] == "user"
        assert messages[2]["role"] == "assistant"
        assert messages[3]["role"] == "user"

    def test_build_messages_does_not_inject_runtime_context(self, tmp_path):
        builder = self._make_builder(tmp_path)
        messages = builder.build_messages(
            history=[],
            current_message="Hello",
            channel="web",
            chat_id="chat-abc123",
            project_id="proj-xyz",
        )
        user_content = messages[-1]["content"]
        assert user_content == "Hello"
        assert "Runtime Context" not in user_content
        assert "chat-abc123" not in user_content
        assert "proj-xyz" not in user_content



class TestRuntimeContextStitching:

    def test_runtime_context_stitched_back_for_llm(self):
        from core.message_history import db_to_llm_history
        rows = [{
            "role": "user",
            "content": "what is 2+2?",
            "metadata": '{"runtime_context": "[Runtime Context]\\nCurrent Time: 2026-05-06 12:00 (Tuesday)"}',
        }]
        out = db_to_llm_history(rows)
        assert len(out) == 1
        assert out[0]["role"] == "user"
        assert out[0]["content"].startswith("what is 2+2?")
        assert "[Runtime Context]" in out[0]["content"]
        assert "Tuesday" in out[0]["content"]

    def test_user_row_without_runtime_context_passes_through_clean(self):
        from core.message_history import db_to_llm_history
        rows = [{"role": "user", "content": "hello", "metadata": None}]
        out = db_to_llm_history(rows)
        assert out == [{"role": "user", "content": "hello"}]



if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
