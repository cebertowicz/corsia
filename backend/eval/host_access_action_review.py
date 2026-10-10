"""Live allow/block matrix for the host-access action reviewer (#2026).

Sends every fixture scenario through the real ``review_action`` path to one
exact Claude Code reviewer model, several times, and fails when any run of
any scenario returns the wrong verdict. It initializes the schema in the
disposable state root (the gate's path checks read it), creates no lease or
thread, and records no usage. Run it only with disposable state roots:

    CORSIA_STATE_DIR=<tmp> CORSIA_WORKSPACE=<tmp> CORSIA_TEST_MODE=1 \\
    CORSIA_ANALYTICS_DISABLE=1 PYTHONPATH=backend \\
    python -m eval.host_access_action_review --state <tmp>/fixture --runs 3 \\
        --model <selector> --reported-model <resolved_identity>

``--model`` is the reviewer's Claude Code selector and ``--reported-model``
must equal the catalog's ``resolved_identity`` for it; the invoker fails
closed on any other reported model. For a before/after comparison, run the
harness from a checkout of each revision.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from eval.host_access_action_review_fixture import (
    Scenario,
    action_payload,
    application,
    scenarios,
)

Invoker = Callable[..., Awaitable[object]]


def reviewer_target(transport_model: str, reported_model: str | None = None) -> Any:
    """Return the Claude Code reviewer target for one exact selector."""
    from llm.resolution import ExecutionTarget

    return ExecutionTarget(
        catalog_id=f"claude-code/{transport_model}",
        harness="claude-code",
        provider="claude-code",
        backend="cc",
        transport_model=transport_model,
        expected_reported_model=reported_model,
        context_window=None,
        available=True,
        availability_reason=None,
        display_name=f"claude-code/{transport_model}",
        supports_reasoning=False,
    )


def matches(expect: str, verdict: str) -> bool:
    """Return whether one verdict satisfies a scenario expectation."""
    if expect == "allow":
        return verdict == "allow"
    return verdict in {"block", "revoke"}


async def _review_once(
    target: Any,
    app: dict[str, Any],
    action: dict[str, Any],
    *,
    binary: str | None,
    invoke: Invoker | None,
) -> tuple[str, str]:
    from host_access.reviewer import review_action

    try:
        review = await review_action(
            target,
            app,
            action,
            binary=binary,
            invoke=invoke,
            usage_thread_id="",
        )
    except Exception as exc:  # a failed review is a matrix error, not a verdict
        return f"error:{type(exc).__name__}", str(exc)[:300]
    return review.verdict, review.reason


def _row(case: Scenario, results: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "number": case.number,
        "label": case.label,
        "expect": case.expect,
        "verdicts": dict(sorted(Counter(v for v, _ in results).items())),
        "passed": all(matches(case.expect, v) for v, _ in results),
        "wrong_reasons": [
            reason for verdict, reason in results
            if not matches(case.expect, verdict)
        ],
    }


async def run_matrix(
    target: Any,
    state: Path,
    *,
    runs: int,
    binary: str | None,
    concurrency: int = 4,
    only: set[int] | None = None,
    invoke: Invoker | None = None,
) -> list[dict[str, Any]]:
    """Review each selected scenario ``runs`` times and summarize verdicts."""
    app = application(state)
    cases = [case for case in scenarios(state) if not only or case.number in only]
    actions = {case.number: action_payload(case) for case in cases}
    gate = asyncio.Semaphore(max(1, concurrency))

    async def _one(case: Scenario) -> tuple[int, tuple[str, str]]:
        async with gate:
            result = await _review_once(
                target, app, actions[case.number], binary=binary, invoke=invoke
            )
        return case.number, result

    outcomes = await asyncio.gather(
        *(_one(case) for _ in range(runs) for case in cases)
    )
    return [
        _row(case, [result for number, result in outcomes if number == case.number])
        for case in cases
    ]


def render(rows: list[dict[str, Any]], *, runs: int) -> str:
    """Render the matrix as a Markdown table."""
    lines = [
        "| # | scenario | expect | verdicts | pass |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        verdicts = ", ".join(f"{k} {v}/{runs}" for k, v in row["verdicts"].items())
        mark = "yes" if row["passed"] else "**NO**"
        lines.append(
            f"| {row['number']} | {row['label']} | {row['expect']} | "
            f"{verdicts} | {mark} |"
        )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", required=True)
    parser.add_argument("--reported-model", required=True)
    parser.add_argument("--binary", default=shutil.which("claude"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--only", type=int, nargs="*")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the matrix, print JSON and a table, and exit 1 on any miss."""
    args = _parser().parse_args(argv)
    if not os.environ.get("CORSIA_STATE_DIR") or os.environ.get(
        "CORSIA_TEST_MODE"
    ) != "1":
        raise SystemExit(
            "Set CORSIA_TEST_MODE=1 and CORSIA_STATE_DIR to a disposable "
            "directory first."
        )
    if not args.binary:
        raise SystemExit("The claude CLI was not found; pass --binary.")
    from infra.database import init_db

    init_db()
    args.state.mkdir(parents=True, exist_ok=True)
    rows = asyncio.run(
        run_matrix(
            reviewer_target(args.model, args.reported_model),
            args.state,
            runs=args.runs,
            binary=args.binary,
            concurrency=args.concurrency,
            only=set(args.only or ()),
        )
    )
    passed = all(row["passed"] for row in rows)
    summary = {
        "model": f"claude-code/{args.model} ({args.reported_model})",
        "runs": args.runs,
        "passed": passed,
        "rows": rows,
    }
    print(json.dumps(summary, indent=2))
    print(render(rows, runs=args.runs))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
