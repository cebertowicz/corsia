from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from eval.default_endpoint import DEFAULT_EVAL_ENDPOINT


def _find_scenario_path(scenario_id: str) -> Path | None:
    repo_root = Path(__file__).resolve().parents[2]
    canonical = repo_root / "evals" / "scenarios" / "canonical"
    if not canonical.is_dir():
        return None
    exact = canonical / f"{scenario_id}.yaml"
    if exact.is_file():
        return exact
    matches = sorted(canonical.glob(f"{scenario_id}*.yaml"))
    if matches:
        return matches[0]
    matches = sorted(canonical.glob(f"*{scenario_id}*.yaml"))
    if matches:
        return matches[0]
    return None


async def _run_scenario(
    scenario_path: Path,
    *,
    endpoint: str | None,
    token: str | None,
    timeout: float,
    log_dir: str | None,
) -> dict[str, Any]:
    from eval.datasets import from_yaml_scenario
    from eval.solvers.yaml_scenario import YamlScenarioSolver
    from eval.inspect_integration import write_scenario_eval_log

    with scenario_path.open("r", encoding="utf-8") as f:
        scenario = yaml.safe_load(f)
    sample = from_yaml_scenario(scenario)

    solver = YamlScenarioSolver(
        endpoint=endpoint,
        token=token,
        timeout=timeout,
    )
    result = await solver.run(sample)

    log_path: Path | None = None
    try:
        log_path = write_scenario_eval_log(
            result,
            log_dir=log_dir,
            task_name=f"corsia.scenario.{result.scenario_id}",
        )
    except Exception as e:
        print(
            f"[eval.run] WARNING: Inspect AI log write failed: {e!s}",
            file=sys.stderr,
        )

    return {
        "scenario_id": result.scenario_id,
        "status": result.status,
        "mode": result.mode,
        "turn_count": len(result.turn_attrs),
        "scorer_outcomes": {
            name: {"passed": o.passed, "explanation": o.explanation}
            for name, o in result.scorer_outcomes.items()
        },
        "errors": result.errors,
        "blocked_scorers": result.blocked_scorers,
        "unknown_scorers": result.unknown_scorers,
        "turn_attrs": result.turn_attrs,
        "inspect_log_path": str(log_path) if log_path is not None else None,
    }


def _format_summary(summary: dict[str, Any]) -> str:
    lines: list[str] = []
    status_emoji = {
        "pass": "PASS",
        "fail": "FAIL",
        "blocked": "BLOCK",
        "error": "ERROR",
    }.get(summary["status"], "?")
    lines.append(
        f"[{status_emoji}] {summary['scenario_id']} (mode={summary['mode']}, "
        f"turns={summary['turn_count']})"
    )
    for name, out in summary["scorer_outcomes"].items():
        mark = "+" if out["passed"] else "-"
        first_line = (out["explanation"] or "").splitlines()[:1]
        snippet = first_line[0] if first_line else ""
        lines.append(f"  {mark} {name}: {snippet}")
    if summary["blocked_scorers"]:
        lines.append(f"  blocked: {summary['blocked_scorers']}")
    if summary["unknown_scorers"]:
        lines.append(f"  unknown: {summary['unknown_scorers']}")
    if summary["errors"]:
        for e in summary["errors"]:
            lines.append(f"  ! {e}")
    if summary["turn_attrs"]:
        for ta in summary["turn_attrs"]:
            cached = ta.get("cached_tokens")
            new_t = ta.get("prefill_new_tokens")
            ttft = ta.get("ttft_ms")
            sph = ta.get("system_prompt_hash")
            sph_short = (sph[:12] + "...") if isinstance(sph, str) and sph else "n/a"
            lines.append(
                f"  turn {ta.get('turn_index', '?')}: cached={cached} "
                f"new={new_t} ttft={ttft} sph={sph_short}"
            )
    if summary["inspect_log_path"]:
        lines.append(f"  log: {summary['inspect_log_path']}")
    return "\n".join(lines)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.eval.run",
        description=(
            "Run a YAML scenario directly through YamlScenarioSolver, "
            "emit an Inspect AI EvalLog, print summary."
        ),
    )
    parser.add_argument(
        "--scenario", required=True,
        help="Scenario id (e.g. 'cache-warm-baseline') or YAML stem.",
    )
    parser.add_argument(
        "--endpoint", default=None,
        help="Eval endpoint URL (default: env CORSIA_EVAL_ENDPOINT or "
             f"{DEFAULT_EVAL_ENDPOINT}).",
    )
    parser.add_argument(
        "--token", default=None,
        help="Auth token (default: env CORSIA_EVAL_TOKEN; no built-in "
             "default -- mint one with `corsia token create --label eval`).",
    )
    parser.add_argument(
        "--timeout", type=float, default=180.0,
        help="HTTP timeout per turn in seconds (default: 180).",
    )
    parser.add_argument(
        "--log-dir", default=None,
        help="Inspect AI log directory (default: ~/.corsia/evals/runs).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Print the full result dict as JSON instead of human summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    scenario_path = _find_scenario_path(args.scenario)
    if scenario_path is None:
        print(
            f"[eval.run] FAILED: no scenario found for id={args.scenario!r}",
            file=sys.stderr,
        )
        return 2

    print(f"[eval.run] scenario={scenario_path.name}")

    try:
        summary = asyncio.run(
            _run_scenario(
                scenario_path,
                endpoint=args.endpoint,
                token=args.token,
                timeout=args.timeout,
                log_dir=args.log_dir,
            ),
        )
    except Exception as e:
        print(f"[eval.run] FAILED: {e!r}", file=sys.stderr)
        return 1

    if args.json:
        import json
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(_format_summary(summary))

    return {
        "pass": 0, "fail": 1, "blocked": 3, "error": 4,
    }.get(summary["status"], 1)


__all__ = ["main", "DEFAULT_INSPECT_LOG_DIR"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


from eval.inspect_integration import DEFAULT_INSPECT_LOG_DIR  # noqa: E402,F401
