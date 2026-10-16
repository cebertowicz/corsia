"""A tool's declared schema and its execute() signature are one contract.

`ToolRegistry.execute` calls `tool.execute(**params)`, where params are the keys
the model supplied -- drawn from the declared JSON schema -- plus the identity
the backend injects under underscore-prefixed names. A named parameter of
`execute()` that appears in neither is unreachable: with no default the call is
a TypeError every time, and with a default it silently takes the default.

Both happened. A rename moved four `execute()` signatures to `thread_id` while
their schemas kept `session_id`, so `delegate_send`, `delegate_status`,
`delegate_set_model` and the thread-history tool raised "missing 1 required
positional argument: 'thread_id'" on every call, and `search_history` quietly
searched every thread instead of the one asked for. Nothing failed, because the
unit tests call `execute()` positionally and never go through the schema.
"""
from __future__ import annotations

import inspect

import pytest

# Identity the registry injects itself (see tools.tool_execution_context.inject_context); these
# are deliberately not declared to the model.
INJECTED_PREFIX = "_"


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """The full harness tool surface, with the optional connector present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(
        "connections.models.connection_available",
        lambda provider, db=None: provider == "claude-code",
    )
    from subagents.harness_tool_bridge import build_tool_registry

    return build_tool_registry(str(workspace))


def _signature(tool):
    """Named parameters of execute(), and whether it absorbs **kwargs."""
    named, absorbs = [], False
    for name, param in inspect.signature(tool.execute).parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            absorbs = True
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        elif name != "self":
            named.append((name, param.default is not inspect.Parameter.empty))
    return named, absorbs


def _schema(tool):
    """Declared property names and the required subset."""
    params = tool.parameters or {}
    props = set(params.get("properties") or {})
    return props, set(params.get("required") or [])


def test_every_execute_parameter_is_declared_or_injected(registry):
    """A parameter the dispatcher cannot bind is a call that cannot succeed."""
    offenders = []
    for name in registry.tool_names:
        tool = registry._tools[name]
        props, _ = _schema(tool)
        for param, has_default in _signature(tool)[0]:
            if param.startswith(INJECTED_PREFIX) or param in props:
                continue
            effect = "silently takes its default" if has_default else "raises TypeError"
            offenders.append(
                f"{name}.execute({param}=...) is declared nowhere in its schema "
                f"{sorted(props)}, so every call {effect}"
            )
    assert not offenders, "\n".join(offenders)


def test_every_required_schema_key_reaches_execute(registry):
    """A required key the tool cannot receive is a promise it cannot keep."""
    offenders = []
    for name in registry.tool_names:
        tool = registry._tools[name]
        _, required = _schema(tool)
        named, absorbs = _signature(tool)
        if absorbs:
            continue
        accepted = {param for param, _ in named}
        for key in sorted(required - accepted):
            offenders.append(
                f"{name} declares '{key}' required, but execute() accepts only "
                f"{sorted(accepted)} and has no **kwargs"
            )
    assert not offenders, "\n".join(offenders)
