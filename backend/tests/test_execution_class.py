
from __future__ import annotations

import sqlite3

import pytest

from infra.execution_class import (
    execution_class,
    is_cc_backed,
    is_codex_backed,
    is_subagent,
)
from llm.resolution import ExecutionTarget

# Subagent-ness derives from a single source of truth: parent presence
# (``spawned_by``). There is no longer a ``session_role`` column.
_COLS = ("id", "model", "spawned_by")


def _row(**fields) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(_COLS)
    placeholders = ", ".join("?" for _ in _COLS)
    conn.execute(f"CREATE TABLE s ({cols})")
    conn.execute(
        f"INSERT INTO s ({cols}) VALUES ({placeholders})",
        tuple(fields.get(c) for c in _COLS),
    )
    return conn.execute("SELECT * FROM s").fetchone()


def _dict(**fields) -> dict:
    return {c: fields.get(c) for c in _COLS}


_SHAPES = [pytest.param(_row, id="sqlite3.Row"), pytest.param(_dict, id="dict")]


def _target(harness: str) -> ExecutionTarget:
    return ExecutionTarget(
        catalog_id="model", harness=harness, provider=harness,
        backend={"native": "api", "claude-code": "cc", "codex": "codex"}[harness],
        transport_model="transport", expected_reported_model=None,
        context_window=1000, available=True, availability_reason=None,
        display_name="model", supports_reasoning=False,
    )


class TestExecutionClass:
    @pytest.mark.parametrize("mk", _SHAPES)
    def test_cc_main_no_parent(self, mk):
        s = mk(id="m", model="claude-code/opus", spawned_by=None)
        assert execution_class(s, target=_target("claude-code")) == "cc_main"
        assert is_cc_backed(s, target=_target("claude-code")) is True
        assert is_subagent(s) is False

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_cc_sub_by_parent(self, mk):
        s = mk(id="s", model="claude-code/sonnet", spawned_by="parent-1")
        assert execution_class(s, target=_target("claude-code")) == "cc_sub"
        assert is_subagent(s) is True

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_null_model_subagent_is_native(self, mk):
        s = mk(id="n", model=None, spawned_by="parent-1")
        assert is_cc_backed(s, target=_target("native")) is False
        assert execution_class(s, target=_target("native")) == "native"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_native_model_thread(self, mk):
        s = mk(id="nat", model="anthropic/opus", spawned_by=None)
        assert is_cc_backed(s, target=_target("native")) is False
        assert execution_class(s, target=_target("native")) == "native"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_native_bare_model(self, mk):
        s = mk(id="b", model="opus", spawned_by=None)
        assert execution_class(s, target=_target("native")) == "native"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_native_subagent_without_provider_stays_native(self, mk):
        s = mk(id="ns", model="anthropic/opus", spawned_by="parent-1")
        assert is_subagent(s) is True
        assert is_cc_backed(s, target=_target("native")) is False
        assert execution_class(s, target=_target("native")) == "native"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_codex_main(self, mk):
        s = mk(id="cx", model="codex/gpt-5-codex", spawned_by=None)
        assert is_codex_backed(s, target=_target("codex")) is True
        assert is_cc_backed(s, target=_target("codex")) is False
        assert execution_class(s, target=_target("codex")) == "codex_main"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_codex_sub_by_parent(self, mk):
        s = mk(id="cxs", model="codex/gpt-5-codex", spawned_by="parent-1")
        assert is_codex_backed(s, target=_target("codex")) is True
        assert execution_class(s, target=_target("codex")) == "codex_sub"

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_cc_gates_before_codex(self, mk):
        s = mk(id="cc", model="claude-code/opus", spawned_by=None)
        assert execution_class(s, target=_target("claude-code")) == "cc_main"
        assert is_codex_backed(s, target=_target("claude-code")) is False

    def test_unexpected_shape_raises(self):
        with pytest.raises(TypeError, match="expected sqlite3.Row or dict"):
            is_cc_backed(42, target=_target("native"))
        with pytest.raises(TypeError):
            execution_class([1, 2, 3], target=_target("native"))

    def test_none_thread_row_is_native(self):
        assert execution_class(None) == "native"
        assert is_cc_backed(None) is False
        assert is_codex_backed(None) is False
        assert is_subagent(None) is False

    def test_absent_required_column_raises(self):
        with pytest.raises(KeyError, match="model"):
            is_cc_backed({"id": "x"})
        with pytest.raises(KeyError, match="spawned_by"):
            is_subagent({"id": "x"})

    @pytest.mark.parametrize("mk", _SHAPES)
    def test_null_column_value_is_not_absent(self, mk):
        s = mk(id="n", model=None, spawned_by=None)
        assert is_cc_backed(s, target=_target("native")) is False
        assert execution_class(s, target=_target("native")) == "native"

    def test_falsy_thread_row_guards_before_field(self):
        assert is_cc_backed({}) is False
        assert is_subagent({}) is False
        assert execution_class(None) == "native"


def test_thread_row_parameter_names_are_canonical():
    """The classification and runtime-truth readers take a CORSIA thread row, named as such."""
    import inspect

    from infra import runtime_truth

    for fn in (execution_class, is_subagent, is_cc_backed, is_codex_backed):
        assert "thread_row" in inspect.signature(fn).parameters, fn.__name__
        assert "session" not in inspect.signature(fn).parameters, fn.__name__
    params = list(inspect.signature(runtime_truth.read_runtime_truth).parameters)
    assert params[:2] == ["thread_id", "thread_row"], params
