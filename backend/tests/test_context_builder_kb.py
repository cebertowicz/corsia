from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_system_prompt_never_auto_retrieves_knowledge_even_with_legacy_query(
    tmp_path, monkeypatch
):
    from core.context_builder import ContextBuilder
    from memory import knowledge_base

    kb_root = tmp_path / "kb"
    facts = kb_root / "facts"
    facts.mkdir(parents=True)
    (facts / "date-tag.md").write_text(
        "---\n"
        "id: kb-1\n"
        "category: facts\n"
        "tags: [auto, 2026-07-02]\n"
        "created_at: 2026-07-02\n"
        "updated_at: 2026-07-02\n"
        "---\n\n"
        "# Date Tag Fact\n\n"
        "This entry should still be injected.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(knowledge_base, "KB_ROOT", kb_root)

    def fail_if_queried(*_args, **_kwargs):
        raise AssertionError("system-prompt assembly must not query the KB")

    monkeypatch.setattr(knowledge_base, "search_entries", fail_if_queried)

    context = ContextBuilder(tmp_path).build_system_prompt(
        kb_query="Date Tag Fact"
    )

    assert "Date Tag Fact" not in context
    assert "[auto, 2026-07-02]" not in context
