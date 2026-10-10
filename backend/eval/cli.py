from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from eval.default_endpoint import DEFAULT_EVAL_BASE, DEFAULT_EVAL_ENDPOINT
from eval.runner import run_eval, _DEFAULT_LOG_DIR


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m corsia.eval",
        description=(
            "Run CORSIA eval harness tasks via Inspect AI. "
            "Tasks live in backend/eval/tasks.py. The eval-friendly CORSIA "
            f"endpoint must be reachable (default {DEFAULT_EVAL_BASE})."
        ),
    )
    parser.add_argument(
        "task",
        nargs="?",
        default=None,
        help="Task name (e.g. 'corsia_latency_under_tool_loops') or "
             "Inspect AI module-path spec. Omit when using --workflow-tasks.",
    )
    parser.add_argument(
        "--workflow-tasks",
        default=None,
        help="Run workflow-eval tasks. Value: 'all' runs every registered "
             "task; otherwise pass a comma-separated list of "
             "task names (e.g. 'corsia_full_session,corsia_error_recovery'). "
             "Tasks live in backend/eval/workflow_tasks/.",
    )
    parser.add_argument(
        "--base", default=None,
        help="(workflow-evals) Base git SHA — informational only; the GHA "
             "gate compares HEAD vs BASE numerically after both runs land "
             "in its log dir.",
    )
    parser.add_argument(
        "--head", default=None,
        help="(workflow-evals) Head git SHA — informational only.",
    )
    parser.add_argument(
        "--model", "-m", default=None,
        help="Optional CORSIA model override (forwarded as metadata.model "
             "to /api/eval/turn).",
    )
    parser.add_argument(
        "--judge-model", default=os.environ.get("CORSIA_EVAL_JUDGE_MODEL"),
        help="Judge model for semantic-equivalence scorers. Defaults to "
             "$CORSIA_EVAL_JUDGE_MODEL; no hardcoded fallback — a judged "
             "task with neither set fails loud at judge time.",
    )
    parser.add_argument(
        "--log-dir", default=_DEFAULT_LOG_DIR,
        help=f"Where to write .eval logs (default: {_DEFAULT_LOG_DIR}).",
    )
    parser.add_argument(
        "--endpoint", default=None,
        help="CORSIA eval endpoint URL "
             f"(default: env CORSIA_EVAL_ENDPOINT or {DEFAULT_EVAL_ENDPOINT}).",
    )
    parser.add_argument(
        "--token", default=None,
        help="CORSIA auth token (default: env CORSIA_EVAL_TOKEN; no "
             "built-in default -- mint one with "
             "`corsia token create --label eval`).",
    )
    parser.add_argument(
        "--limit", "-n", type=int, default=None,
        help="Cap the dataset to N samples.",
    )
    parser.add_argument(
        "--no-view", action="store_true",
        help="Don't print the 'inspect view' command after the eval finishes.",
    )
    return parser.parse_args(argv)


def _resolve_workflow_task_names(spec: str) -> list[str]:
    from eval.workflow_tasks import WORKFLOW_TASKS

    if spec.strip().lower() == "all":
        return list(WORKFLOW_TASKS.keys())
    names = [n.strip() for n in spec.split(",") if n.strip()]
    unknown = [n for n in names if n not in WORKFLOW_TASKS]
    if unknown:
        raise ValueError(
            f"unknown workflow task(s): {unknown}. Known: {sorted(WORKFLOW_TASKS)}",
        )
    return names


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.endpoint:
        os.environ["CORSIA_EVAL_ENDPOINT"] = args.endpoint
    if args.token:
        os.environ["CORSIA_EVAL_TOKEN"] = args.token

    if args.workflow_tasks is None and args.task is None:
        print(
            "[corsia.eval] ERROR: provide a task name OR --workflow-tasks=<spec>",
            file=sys.stderr,
        )
        return 2

    if args.workflow_tasks is not None:
        try:
            task_names = _resolve_workflow_task_names(args.workflow_tasks)
        except ValueError as e:
            print(f"[corsia.eval] {e}", file=sys.stderr)
            return 2
        print(f"[corsia.eval] workflow_tasks={task_names}")
        print(f"[corsia.eval] base={args.base} head={args.head}")
        print(f"[corsia.eval] judge_model={args.judge_model}")
        print(f"[corsia.eval] log_dir={args.log_dir}")
        print()
        all_logs: list[Any] = []
        for name in task_names:
            print(f"[corsia.eval] running workflow task: {name}")
            try:
                logs = run_eval(
                    name,
                    model=args.model,
                    judge_model=args.judge_model,
                    log_dir=args.log_dir,
                    limit=args.limit,
                )
            except Exception as e:
                print(f"[corsia.eval] {name} FAILED: {e!s}", file=sys.stderr)
                return 1
            all_logs.extend(logs)
        logs = all_logs
    else:
        print(f"[corsia.eval] task={args.task}")
        print(f"[corsia.eval] endpoint={os.environ.get('CORSIA_EVAL_ENDPOINT', DEFAULT_EVAL_ENDPOINT)}")
        print(f"[corsia.eval] judge_model={args.judge_model}")
        print(f"[corsia.eval] log_dir={args.log_dir}")
        if args.limit is not None:
            print(f"[corsia.eval] limit={args.limit}")
        print()

        try:
            logs = run_eval(
                args.task,
                model=args.model,
                judge_model=args.judge_model,
                log_dir=args.log_dir,
                limit=args.limit,
            )
        except Exception as e:
            print(f"[corsia.eval] FAILED: {e!s}", file=sys.stderr)
            return 1

    print(f"[corsia.eval] completed; produced {len(logs)} log(s).")
    for log in logs:
        loc = getattr(log, "location", None) or getattr(log, "path", None)
        if loc:
            print(f"[corsia.eval]   log: {loc}")

    if not args.no_view:
        print()
        print("[corsia.eval] To view results in the Inspect AI HTML viewer:")
        print(f"[corsia.eval]   inspect view --log-dir {args.log_dir}")

    return 0


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
