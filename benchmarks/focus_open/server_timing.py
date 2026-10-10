#!/usr/bin/env python3
"""Per-page server time for the seeded threads, split by stage, no product edits.

Run from ``backend/`` (PYTHONPATH=.) with the SAME disposable CORSIA_WORKSPACE /
CORSIA_STATE_DIR the seed used. It walks the ``/messages`` cursor chain in
process, ``--pages`` pages of 50 anchors, ``--reps`` times, and wraps the
paging functions plus the connection to time:

  anchors   - _fetch_display_anchors (the main page query)
  lineage   - _fetch_lineage_rows (tool-lineage child queries), of which
  like      - the legacy ``metadata LIKE`` assistant scan inside it
  project   - the rest of page_messages (display projection, delivery intents)
  summaries - page_turn_summaries

Usage: server_timing.py threads.json [--pages N] [--reps R]
"""
from __future__ import annotations

import argparse
import json
import statistics
import time

from api import thread_paging as tp
from infra.database import get_db

acc: dict[str, float] = {}


def timed(name, fn):
    def wrapper(*a, **kw):
        t = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            acc[name] = acc.get(name, 0.0) + (time.perf_counter() - t) * 1000
    return wrapper


class DbProxy:
    """Times the legacy LIKE scan; delegates everything else."""

    def __init__(self, db):
        self._db = db

    def execute(self, sql, *a, **kw):
        if "metadata LIKE" in sql:
            t = time.perf_counter()
            try:
                return self._db.execute(sql, *a, **kw)
            finally:
                acc["like"] = acc.get("like", 0.0) + (time.perf_counter() - t) * 1000
        return self._db.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._db, name)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("threads")
    ap.add_argument("--pages", type=int, default=13)
    ap.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()
    threads = json.load(open(args.threads))

    tp._fetch_display_anchors = timed("anchors", tp._fetch_display_anchors)
    tp._fetch_lineage_rows = timed("lineage", tp._fetch_lineage_rows)
    summaries = timed("summaries", tp.page_turn_summaries)
    page_messages = timed("page_total", tp.page_messages)

    out = {}
    for label, tid in threads.items():
        per_page = []  # one dict per page index, median over reps
        reps: list[list[dict]] = []
        for _ in range(args.reps):
            db = DbProxy(get_db())
            before, rows = None, []
            for _p in range(args.pages):
                acc.clear()
                display, has_more, oldest = page_messages(db, tid, 50, before, include_tool_results=False)
                summaries(db, tid, display)
                acc["project"] = acc["page_total"] - acc.get("anchors", 0) - acc.get("lineage", 0)
                rows.append(dict(acc))
                if not has_more:
                    break
                before = oldest
            reps.append(rows)
        n_pages = min(len(r) for r in reps)
        for i in range(n_pages):
            per_page.append({k: statistics.median(r[i].get(k, 0.0) for r in reps)
                             for k in ("page_total", "anchors", "lineage", "like", "project", "summaries")})
        out[label] = per_page
        tot = {k: round(sum(p[k] for p in per_page), 1) for k in per_page[0]}
        mean = {k: round(v / len(per_page), 1) for k, v in tot.items()}
        print(f"{label:22s} pages={len(per_page):2d} per-page mean ms {mean}  chain total ms {tot}")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
