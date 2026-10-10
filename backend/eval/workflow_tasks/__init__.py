from __future__ import annotations

from typing import Any


def _load_task(task_name: str) -> Any:
    from . import task_full_session, task_multi_turn_with_tools, task_error_recovery
    mapping = {
        "corsia_full_session": task_full_session.corsia_full_session,
        "corsia_multi_turn_with_tools": task_multi_turn_with_tools.corsia_multi_turn_with_tools,
        "corsia_error_recovery": task_error_recovery.corsia_error_recovery,
    }
    return mapping[task_name]


class _WorkflowTaskRegistry:

    _names = (
        "corsia_full_session",
        "corsia_multi_turn_with_tools",
        "corsia_error_recovery",
    )

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._names

    def __getitem__(self, name: str) -> Any:
        if name not in self._names:
            raise KeyError(name)
        return _load_task(name)

    def __iter__(self):
        return iter(self._names)

    def keys(self):
        return list(self._names)

    def items(self):
        return [(n, _load_task(n)) for n in self._names]

    def values(self):
        return [_load_task(n) for n in self._names]

    def __len__(self) -> int:
        return len(self._names)


WORKFLOW_TASKS = _WorkflowTaskRegistry()


def __getattr__(name: str) -> Any:
    if name in WORKFLOW_TASKS:
        return _load_task(name)
    raise AttributeError(f"module 'eval.workflow_tasks' has no attribute {name!r}")


__all__ = [
    "corsia_full_session",
    "corsia_multi_turn_with_tools",
    "corsia_error_recovery",
    "WORKFLOW_TASKS",
]
