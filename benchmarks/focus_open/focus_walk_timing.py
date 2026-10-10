#!/usr/bin/env python3
"""Server time of the Focus page walk on the seeded threads.

Run from ``backend/`` (PYTHONPATH=.) with the SAME disposable CORSIA_WORKSPACE /
CORSIA_STATE_DIR the seed used. For each thread and scan cap it walks the
``view=focus`` cursor chain in process (``--pages`` pages of 50 anchors,
``--reps`` times) inside ``read_authorized_focus_page``'s read snapshot and
reports, per page: the whole page time, the walk time, and the rows the walk
scanned. ``ms/row`` is walk time over rows scanned; p95 is over every page.

Usage: focus_walk_timing.py threads.json [--caps 1000,2000,4000] [--pages 12] [--reps 5]
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from api import thread_focus_page as tf
from infra.database import get_db

walks: list[tuple[float, int]] = []
_orig_walk = tf._walk


def _timed_walk(*a, **kw):
    t = time.perf_counter()
    walk = _orig_walk(*a, **kw)
    walks.append(((time.perf_counter() - t) * 1000, walk.scanned))
    return walk


def p95(xs: list[float]) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("threads")
    ap.add_argument("--caps", default="4000")
    ap.add_argument("--pages", type=int, default=12)
    ap.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()
    tf._walk = _timed_walk
    db = get_db()
    for label, tid in json.load(open(args.threads)).items():
        for cap in (int(c) for c in args.caps.split(",")):
            page_ms: list[float] = []
            walk_ms: list[float] = []
            scanned: list[int] = []
            for _ in range(args.reps):
                before = None
                for _p in range(args.pages):
                    walks.clear()
                    t = time.perf_counter()
                    page = tf.read_authorized_focus_page(db, tid, 50, before, scan_cap=cap)
                    page_ms.append((time.perf_counter() - t) * 1000)
                    walk_ms.append(walks[0][0])
                    scanned.append(walks[0][1])
                    if not page["has_more"]:
                        break
                    before = page["oldest_id"]
            rows = sum(scanned)
            print(
                f"{label:28s} cap={cap:6d} pages/rep={len(page_ms) // args.reps:3d} "
                f"scanned/page={statistics.mean(scanned):7.0f} "
                f"walk ms mean={statistics.mean(walk_ms):6.1f} p95={p95(walk_ms):6.1f} "
                f"page ms p95={p95(page_ms):6.1f}  ms/row={sum(walk_ms) / rows:.4f}"
            )


if __name__ == "__main__":
    main()
