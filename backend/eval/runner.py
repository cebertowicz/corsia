from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from inspect_ai import eval as ia_eval, eval_async as ia_eval_async
from inspect_ai.log import EvalLog


_JUDGE_MODEL_ENV = "CORSIA_EVAL_JUDGE_MODEL"


def _default_judge_model() -> str:
    judge = os.environ.get(_JUDGE_MODEL_ENV)
    if judge and judge.strip():
        return judge.strip()
    raise RuntimeError(
        f"No eval judge model configured. Set {_JUDGE_MODEL_ENV} "
        "(the workflow gate passes --judge-model / sets this env var). "
        "No hardcoded default model is used."
    )


_DEFAULT_LOG_DIR = os.environ.get(
    "CORSIA_EVAL_LOG_DIR",
    str(Path.home() / ".corsia" / "logs" / "eval"),
)

_DEFAULT_SUT_MODEL = os.environ.get("CORSIA_EVAL_SUT_MODEL", "none/none")

_TASK_CACHE: dict[str, Any] = {}


def _resolve_task(task_name: str) -> Any:
    if task_name in _TASK_CACHE:
        return _TASK_CACHE[task_name]

    try:
        from eval import tasks as task_module
        candidate = getattr(task_module, task_name, None)
        if callable(candidate):
            _TASK_CACHE[task_name] = candidate
            return candidate
    except ImportError:
        pass

    try:
        from eval.workflow_tasks import WORKFLOW_TASKS
        if task_name in WORKFLOW_TASKS:
            candidate = WORKFLOW_TASKS[task_name]
            _TASK_CACHE[task_name] = candidate
            return candidate
    except ImportError:
        pass

    return task_name


def _ensure_log_dir(log_dir: str | None) -> str:
    final = log_dir or _DEFAULT_LOG_DIR
    Path(final).mkdir(parents=True, exist_ok=True)
    return final


def run_eval(
    task_name: str,
    *,
    model: str | None = None,
    judge_model: str | None = None,
    log_dir: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[EvalLog]:
    resolved = _resolve_task(task_name)
    final_log_dir = _ensure_log_dir(log_dir)

    if on_progress is not None:
        on_progress({"phase": "start", "task": task_name})

    eval_kwargs: dict[str, Any] = {
        "log_dir": final_log_dir,
        "model": model or _DEFAULT_SUT_MODEL,
    }
    if judge_model is not None:
        judge = judge_model or _default_judge_model()
        eval_kwargs["model_roles"] = {"grader": judge, "judge": judge}
    if limit is not None:
        eval_kwargs["limit"] = limit
    eval_kwargs.update(kwargs)

    logs = ia_eval(resolved, **eval_kwargs)

    if on_progress is not None:
        on_progress({"phase": "end", "task": task_name, "log_count": len(logs)})

    return logs


async def stream_eval(
    task_name: str,
    *,
    model: str | None = None,
    judge_model: str | None = None,
    log_dir: str | None = None,
    limit: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    **kwargs: Any,
) -> list[EvalLog]:
    resolved = _resolve_task(task_name)
    final_log_dir = _ensure_log_dir(log_dir)

    if on_progress is not None:
        on_progress({"phase": "start", "task": task_name})

    eval_kwargs: dict[str, Any] = {
        "log_dir": final_log_dir,
        "model": model or _DEFAULT_SUT_MODEL,
    }
    if judge_model is not None:
        judge = judge_model or _default_judge_model()
        eval_kwargs["model_roles"] = {"grader": judge, "judge": judge}
    if limit is not None:
        eval_kwargs["limit"] = limit
    eval_kwargs.update(kwargs)

    logs = await ia_eval_async(resolved, **eval_kwargs)

    if on_progress is not None:
        on_progress({"phase": "end", "task": task_name, "log_count": len(logs)})

    return logs


__all__ = ["run_eval", "stream_eval", "_default_judge_model", "_DEFAULT_LOG_DIR"]
