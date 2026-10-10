#!/usr/bin/env python3
"""Seed disposable threads for the Focus-open baseline.

Run from ``backend/`` with a DISPOSABLE ``CORSIA_WORKSPACE`` / ``CORSIA_STATE_DIR``
(``CORSIA_TEST_MODE=1``) and a backend already serving that state. Threads are
created through the real ``POST /api/threads`` route; message and turn rows are
then written with one ``executemany`` because ``insert_message`` refuses tool
calls on a terminal Turn, and 10,000 single-row write transactions are not the
thing being measured. Row shapes follow ``tests/test_portable_history.py``.

N counts display anchors (user / assistant / system rows, what ``/messages``
pages over); tool-result rows are added on top. ``visible`` is the fraction of
anchors Focus shows: human messages, ``for_user`` (post_update) assistant rows
and system notices. Of the hidden anchors, 70% are tool-calling steps (each
with one result row, 20% of those "legacy": no ``assistant_step_id``, so the
``metadata LIKE`` lineage scan runs) and 30% are plain assistant working text.

``--prod-bodies`` sizes bodies like production (14-day aggregate, 2026-10-04): assistant
rows average about 4.5 KB with a 13 KB p95, system rows about 5.8 KB. Hidden tool steps carry
their bulk in metadata (reasoning), hidden text rows in content, so a walk that reads a
skipped row's metadata pays for the overflow pages in front of it.

Prints a JSON map ``{label: thread_id}``.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

SIZES = (500, 2000, 10000)
RATIOS = {"1pct": 0.01, "5pct": 0.05, "0p7pct": 0.007, "2pct": 0.02}
DEFAULT_RATIOS = ("1pct", "5pct")
MODEL = "mock/test-model"


def _post(api: str, token: str, title: str) -> str:
    req = urllib.request.Request(
        f"{api}/api/threads?token={token}",
        data=json.dumps({"title": title, "model": MODEL}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["thread_id"]


def _body(rng: random.Random, mean: int, ch: str) -> str:
    """A lognormal body: p95 is ~2.9x the mean, matching production's assistant rows."""
    import math
    sigma = 0.881
    return ch * max(40, int(rng.lognormvariate(math.log(mean) - sigma * sigma / 2, sigma)))


def _tool_step_meta(rng: random.Random, turn_id: str | None, call_id: str, call_no: int,
                    prod: bool) -> dict:
    """Metadata of a hidden tool-calling step; production steps carry their reasoning here."""
    return {
        "turn_id": turn_id, "step_type": "tool_calls",
        **({"reasoning": _body(rng, 4500, "r")} if prod else {}),
        "llm_tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": "Read", "arguments": json.dumps({"path": f"src/file{call_no}.py"})},
        }],
    }


def build_rows(thread_id: str, n: int, visible: float, rng: random.Random, tail: str,
               legacy_share: float = 0.2, prod: bool = False):
    """Return (turns, rows) for one thread; rows are messages-insert tuples."""
    period = max(2, round(1 / visible))
    kinds = ["user", "update", "notice", "update"]
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    clock = 0
    seq = 0
    turn_no = 0
    turn_id = None
    rows: list[tuple] = []
    turns: list[tuple] = []

    def ts() -> str:
        nonlocal clock
        clock += 1
        return (base + timedelta(seconds=clock)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def add(role, content, meta, mid=None):
        nonlocal seq
        seq += 1
        mid = mid or f"{thread_id}-m{seq}"
        rows.append((mid, thread_id, role, content, json.dumps(meta) if meta else None, ts(), seq))
        return mid

    def open_turn():
        nonlocal turn_no, turn_id
        turn_no += 1
        turn_id = f"{thread_id}-t{turn_no}"
        stamp = ts()
        turns.append((turn_id, thread_id, MODEL, stamp, stamp))

    anchors = 0
    visible_index = 0
    call_no = 0
    while anchors < n:
        if tail == "idle":
            # The agent has just spoken: the newest anchor is a visible update.
            is_visible = anchors == 0 or anchors == n - 1 or anchors % period == period - 1
        else:
            # The agent is mid-work: visible items sit at multiples of the period,
            # so the newest rows are hidden work since the last one.
            is_visible = anchors % period == 0
        if is_visible:
            kind = "user" if anchors == 0 else kinds[visible_index % len(kinds)]
            if tail == "idle" and anchors == n - 1:
                kind = "update"
            visible_index += 1
            if kind == "user":
                open_turn()
                add("user", f"Please continue with step {anchors}: " + "x" * rng.randint(40, 200),
                    {"turn_id": turn_id})
            elif kind == "update":
                body = _body(rng, 4500, "y") if prod else "y" * rng.randint(80, 300)
                add("assistant", "Progress update. " + body,
                    {"for_user": True, "turn_id": turn_id})
            else:
                body = _body(rng, 5800, "z") if prod else "z" * 40
                add("system", "Notice: a step needed attention. " + body, {"type": "error"})
            anchors += 1
            continue
        if rng.random() < 0.7:
            call_no += 1
            call_id = f"call-{thread_id}-{call_no}"
            step_id = f"{thread_id}-m{seq + 1}"
            add("assistant", "", _tool_step_meta(rng, turn_id, call_id, call_no, prod), mid=step_id)
            anchors += 1
            result = {"tool_call_id": call_id, "name": "Read", "turn_id": turn_id}
            if rng.random() >= legacy_share:
                result["assistant_step_id"] = step_id
            add("tool", "result line\n" * rng.randint(20, 120), result)
        else:
            text = _body(rng, 4500, "w") if prod else "w" * rng.randint(60, 600)
            add("assistant", "Working through it. " + text, {"turn_id": turn_id})
            anchors += 1
    return turns, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api", required=True, help="disposable backend base URL")
    ap.add_argument("--token-file", required=True, help="file holding the disposable account token")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--legacy-share", type=float, default=0.2,
                    help="share of tool results written without assistant_step_id (legacy rows)")
    ap.add_argument("--ratios", default=",".join(DEFAULT_RATIOS),
                    help="comma list of " + ",".join(RATIOS))
    ap.add_argument("--prod-bodies", action="store_true",
                    help="production-sized message bodies (see the module docstring)")
    ap.add_argument("--tail", choices=("idle", "working"), default="idle",
                    help="idle: thread ends on a visible update; working: ends on hidden work")
    ap.add_argument("--sizes", default=",".join(map(str, SIZES)))
    args = ap.parse_args()
    token = open(args.token_file).read().strip()

    from infra.database import get_db
    from infra.time import utc_now_iso

    db = get_db()
    out: dict[str, str] = {}
    for n in (int(s) for s in args.sizes.split(",")):
        for rlabel, ratio in ((r, RATIOS[r]) for r in args.ratios.split(",")):
            label = (f"{n}-{rlabel}-{args.tail}" + ("-prod" if args.prod_bodies else "")
                     + (f"-legacy{args.legacy_share:g}" if args.legacy_share != 0.2 else ""))
            thread_id = _post(args.api, token, f"focus-baseline {label}")
            rng = random.Random(f"{args.seed}-{label}")
            turns, rows = build_rows(
                thread_id, n, ratio, rng, args.tail, args.legacy_share, args.prod_bodies,
            )
            db.executemany(
                "INSERT INTO active_turns (id,thread_id,execution_epoch,model,status,started_at,"
                "ended_at,boot_epoch) VALUES (?,?,0,?, 'completed',?,?,'focus-baseline')",
                turns,
            )
            db.executemany(
                "INSERT INTO messages (id,thread_id,role,content,metadata,created_at,seq) "
                "VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            db.execute(
                "UPDATE threads SET next_message_seq=?, updated_at=? WHERE id=?",
                (len(rows) + 1, utc_now_iso(), thread_id),
            )
            db.commit()
            out[label] = thread_id
            print(f"{label}: {thread_id} rows={len(rows)} turns={len(turns)}", file=sys.stderr)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
