from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_scores_from_log_dir(log_dir: Path) -> dict[str, float | None]:
    try:
        from inspect_ai.log import read_eval_log
    except ImportError as e:
        raise SystemExit(f"inspect_ai required to read eval logs: {e}")

    out: dict[str, list[float]] = {}
    for path in sorted(log_dir.glob("*.eval")):
        try:
            log = read_eval_log(str(path))
        except Exception as e:
            print(f"[compare] could not read {path}: {e}", file=sys.stderr)
            continue
        task_name = getattr(log.eval, "task", None) or path.stem
        if isinstance(task_name, str) and "@" in task_name:
            task_name = task_name.split("@", 1)[-1]
        scores: list[float] = []
        for sample in (getattr(log, "samples", None) or []):
            for score in (getattr(sample, "scores", None) or {}).values():
                meta = getattr(score, "metadata", None) or {}
                if not isinstance(meta, dict):
                    continue
                val = meta.get("score")
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    scores.append(float(val))
        if scores:
            out.setdefault(task_name, []).extend(scores)
        else:
            out.setdefault(task_name, [])

    return {
        name: (sum(vals) / len(vals)) if vals else None
        for name, vals in out.items()
    }


def compare(
    *,
    base_scores: dict[str, float | None],
    head_scores: dict[str, float | None],
    drop_pct: float,
) -> tuple[bool, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    regressed = False
    task_names = sorted(set(base_scores) | set(head_scores))
    drop_frac = drop_pct / 100.0

    for name in task_names:
        base = base_scores.get(name)
        head = head_scores.get(name)
        delta = (head - base) if (base is not None and head is not None) else None
        is_regression = False
        if base is not None and head is not None:
            if head < (base - drop_frac):
                is_regression = True
                regressed = True
        rows.append({
            "task": name,
            "base": base,
            "head": head,
            "delta": delta,
            "regression": is_regression,
        })

    return regressed, rows


def _format_rows(rows: list[dict[str, Any]], drop_pct: float) -> str:
    lines = [
        f"Drop threshold: {drop_pct:.1f}% (HEAD must be ≥ BASE - {drop_pct/100:.3f})",
        "",
        f"{'task':<48} {'base':>8} {'head':>8} {'delta':>8} {'status':>10}",
        "-" * 86,
    ]
    for r in rows:
        base = f"{r['base']:.3f}" if isinstance(r['base'], float) else "—"
        head = f"{r['head']:.3f}" if isinstance(r['head'], float) else "—"
        delta = f"{r['delta']:+.3f}" if isinstance(r['delta'], float) else "—"
        status = "REGRESSION" if r["regression"] else (
            "missing" if r["base"] is None or r["head"] is None else "ok"
        )
        lines.append(f"{r['task']:<48} {base:>8} {head:>8} {delta:>8} {status:>10}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m eval.workflow_tasks.compare",
        description="Gate workflow-eval HEAD vs BASE on per-task judge-score regression.",
    )
    parser.add_argument("--base-log-dir", required=True, type=Path)
    parser.add_argument("--head-log-dir", required=True, type=Path)
    parser.add_argument(
        "--drop-pct", type=float, default=5.0,
        help="Regression threshold in percentage points (default: 5).",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON instead of the table.",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    if not args.base_log_dir.exists():
        print(f"[compare] base-log-dir missing: {args.base_log_dir}", file=sys.stderr)
        return 0  # Permissive on missing data; GHA step logs a warning.
    if not args.head_log_dir.exists():
        print(f"[compare] head-log-dir missing: {args.head_log_dir}", file=sys.stderr)
        return 0

    base = _load_scores_from_log_dir(args.base_log_dir)
    head = _load_scores_from_log_dir(args.head_log_dir)

    if not base and not head:
        print("[compare] both log dirs empty — nothing to compare. Pass.", file=sys.stderr)
        return 0

    regressed, rows = compare(
        base_scores=base, head_scores=head, drop_pct=args.drop_pct,
    )

    if args.json:
        print(json.dumps({
            "drop_pct": args.drop_pct,
            "regressed": regressed,
            "rows": rows,
        }, indent=2))
    else:
        print(_format_rows(rows, args.drop_pct))

    return 1 if regressed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["compare", "main"]
