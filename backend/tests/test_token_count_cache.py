"""Tokenizer work for the next-turn preview: counted once, bounded, one measure.

These tests count encode calls through a spy encoding; they never time the
preview, so they hold on a slow or loaded host.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from test_context_preview_contracts import (
    _patch_preview_sources,
    _preview_connection,
    _target,
)


class _SpyEncoding:
    """One token per character; records every text it is asked to encode."""

    name = "spy_base"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def encode(self, text: str, *, disallowed_special=()) -> list[int]:
        self.calls.append(text)
        return [0] * len(text)


@pytest.fixture
def spy(monkeypatch):
    from core import token_estimation
    from core.token_count_cache import TOKEN_COUNTS

    encoding = _SpyEncoding()
    monkeypatch.setattr(token_estimation, "_model_encoding", lambda _model: encoding)
    TOKEN_COUNTS.clear()
    yield encoding
    TOKEN_COUNTS.clear()


def _source(content: str, **overrides):
    from core.effective_context import ContextSourceEntry

    fields = dict(
        source_id="project-context:corsia:context/PROJECT.md",
        origin_section="project",
        label="Project bootloader",
        stable_reference="/workspace/context/PROJECT.md",
        freshness="fresh",
        inclusion="included",
        reason="project bootloader",
        owner_surface="document-viewer",
        prompt_content=content,
    )
    fields.update(overrides)
    return ContextSourceEntry(**fields)


def test_cache_counts_each_text_once_and_recounts_an_edit(spy):
    from core.token_count_cache import TokenCountCache

    cache = TokenCountCache(max_entries=8)
    assert cache.count(spy, "history row") == 11
    assert cache.count(spy, "history row") == 11
    assert spy.calls == ["history row"]

    # An edited row is new content, so it can never read the old count.
    assert cache.count(spy, "history row, edited") == 19
    assert spy.calls == ["history row", "history row, edited"]


def test_cache_is_bounded_and_evicts_least_recently_used(spy):
    from core.token_count_cache import TokenCountCache

    cache = TokenCountCache(max_entries=2)
    cache.count(spy, "a")
    cache.count(spy, "b")
    cache.count(spy, "a")  # refreshes "a"; "b" is now oldest
    cache.count(spy, "c")
    assert len(cache) == 2

    spy.calls.clear()
    cache.count(spy, "a")
    cache.count(spy, "b")
    assert spy.calls == ["b"]


def test_cache_separates_encodings_for_the_same_text(spy):
    from core.token_count_cache import TokenCountCache

    other = _SpyEncoding()
    other.name = "spy_other"
    cache = TokenCountCache()
    cache.count(spy, "same text")
    cache.count(other, "same text")
    assert spy.calls == ["same text"]
    assert other.calls == ["same text"]


def test_second_identical_preview_does_not_re_encode(monkeypatch, spy):
    from core import context_preview

    connection, thread_row = _preview_connection()
    connection.executemany(
        "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
        "VALUES (?,'preview-thread',?,?,?,'t',?)",
        [
            ("m1", "user", "first question " * 50, json.dumps({}), 1),
            ("m2", "assistant", "first answer " * 50, json.dumps({}), 2),
        ],
    )
    target = _target()
    _patch_preview_sources(monkeypatch, target)
    sources = [_source("PROJECT BOOTLOADER " * 40)]
    monkeypatch.setattr(
        context_preview,
        "collect_context_sources",
        lambda **_kwargs: SimpleNamespace(
            sources=sources, agent_facts={}, installed=None, project_cwd=None,
        ),
    )

    first = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
    )
    encoded = list(spy.calls)
    assert any("first question" in text for text in encoded)
    assert any("PROJECT BOOTLOADER" in text for text in encoded)

    spy.calls.clear()
    second = context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
    )
    assert second == first
    assert not [text for text in spy.calls if "first question" in text]
    assert not [text for text in spy.calls if "PROJECT BOOTLOADER" in text]

    # Editing a row recounts that row only.
    connection.execute(
        "UPDATE messages SET content = ? WHERE id = 'm1'", ("edited question " * 50,),
    )
    spy.calls.clear()
    context_preview.build_next_turn_context_preview(
        "preview-thread", thread_row, db=connection,
    )
    assert [text for text in spy.calls if "edited question" in text]
    assert not [text for text in spy.calls if "first answer" in text]


@pytest.mark.parametrize(
    "source",
    [
        _source("PROJECT BOOTLOADER " * 40),
        _source("overridden body", prompt_tokens_override=1234),
    ],
    ids=["counted", "override"],
)
def test_inventory_row_and_preflight_count_a_source_identically(spy, source):
    from core.context_admission import _include_managed_sources, _PreflightUsage

    usage = _PreflightUsage(0, 0, 0, {}, 0)
    _include_managed_sources(usage, [source], model="native-model")

    row = source.to_inventory_dict(model="native-model")
    assert row["prompt_tokens"] == usage.source_tokens
    assert usage.source_section_tokens == {"project": row["prompt_tokens"]}
